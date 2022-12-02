import torch
from torch._ops import PyOperator
from torch._C._functorch import TransformType
from torch._functorch.utils import enable_autograd_function
from torch.autograd.function import _SingleLevelFunction
import torch.utils._pytree as pytree
from torch._C._functorch import (
    _wrap_for_grad,
    _unwrap_for_grad,
    _unwrap_batched,
)
from functorch._src.vmap import (
    _broadcast_to_and_flatten,
    _create_batched_inputs,
)
from torch.autograd.forward_ad import _enable_fwd_grad
from typing import NamedTuple

# autograd.Function technically runs before the regular PyTorch dispatcher.
# This is how features like autocast and torch_dispatch (e.g. PythonTLSSnapshot)
# work with it. One day we might decide to change this, but until then,
# we need to give the illusion that autograd.Function runs before those things.
#
# We do this by using creating a custom PyOperator that only functorch
# dispatches specially.
class CustomFunctionPyOperator(PyOperator):
    def __init__(self):
        super().__init__('custom_function_call')

    def __call__(self, *args, **kwargs):
        # When custom_function_call is done dispatching through functorch,
        # it should just invoke the autograd.Function. This is consistent
        # with the autograd.Function behavior of being invoked before the
        # PyTorch dispatcher.
        #
        # This will lead us into trouble later down the line, but this is
        # pre-existing. There is an invariant that a function traced by
        # make_fx should have the same behavior when provided the same
        # Tensor. However, make_fx sees autograd.Function as a composite
        # (because autograd.Function happens before the Python dispatch key)
        # and only traces the forward pass.
        if torch._C._are_functorch_transforms_active():
            return super().__call__(*args, **kwargs)
        autograd_function = args[0]
        return autograd_function.apply(*args[1:], **kwargs)


# "custom_function_call"
# This is the mechanism for an autograd.Function that works with functorch transforms.
# It wraps an autograd.Function; interactions with functorch transforms are defined
# via PyDispatcher and PyOperator rather than through the traditional PyTorch
# dispatcher.
custom_function_call = CustomFunctionPyOperator()


# The grad rule for custom_function_call is to construct a new _SingleLevelFunction
# (autograd.Function that only works with a single layer (level) of functorch) that:
# - unwraps the inputs
# - redispatches to custom_function_call
# - wraps the outputs
# and whose backward pass calls the original autograd.Function's backward.
#
# Why do we need to redispatch to custom_function_call?
# -----------------------------------------------------
# This is consistent with how ATen operators work with functorch's grad transform:
# they always redispatch to the original operator.
# Consider torch.sin, and let's say we do grad0(grad1(torch.sin))(x)
#
# grad1 will:
# - set up the autograd graph
# - unwrap the inputs
# - redispatch to at::sin (*)
# - rewrap the outputs on the return
#
# On the redispatch in (*), grad0 will:
# - set up the autograd graph
# - unwrap the inputs
# - redispatch to at::sin
# - rewrap the outputs on the return
#
# To "set up the autograd graph", we generate a _SingleLevelFunction
# and apply it.
@custom_function_call.py_impl(TransformType.Grad)
@custom_function_call.py_impl(TransformType.Jvp)
def custom_function_call_grad(interpreter, autograd_function, *operands):
    maybe_interpreter = interpreter
    level = maybe_interpreter.level()

    # TODO: The name of the grad_fn is GeneratedBackward. This isn't a great UX,
    # but in theory functorch users shouldn't be peeking at the grad_fn.
    # We should try to generate a better name for this.
    class Generated(_SingleLevelFunction):
        @staticmethod
        def forward(*operands):
            unwrapped_operands = pytree.tree_map_only(
                torch.Tensor,
                lambda x: _unwrap_for_grad(x, level),
                operands)
            # Both enable_grad() and _enable_fwd_grad() are necessary no matter
            # the transform. _SingleLevelFunction will turn off both fwd and bwd
            # gradient computation and we need to turn it back on here.
            with torch.enable_grad(), _enable_fwd_grad(), maybe_interpreter.lower():
                output = custom_function_call(autograd_function, *unwrapped_operands)

            # autograd.Function's ctx.mark_dirty expect a returned input
            # to have the same object identity as the input.
            # Mode-only functorch will greatly simplify this logic.
            results = wrap_outputs_maintaining_identity(
                output,
                unwrapped_operands,
                operands,
                level)
            return results

        @staticmethod
        def setup_context(ctx, outputs, *operands):
            return autograd_function.setup_context(ctx, outputs, *operands)

        # backward is only used if the transform is TransformType.Grad
        @staticmethod
        def backward(ctx, *grads):
            result = autograd_function.backward(ctx, *grads)
            return result

        # jvp is only used if the transform is TransformType.Jvp
        @staticmethod
        def jvp(ctx, *tangents):
            result = autograd_function.jvp(ctx, *tangents)
            return result

    with enable_autograd_function():
        flat_out = Generated.apply(*operands)
    return flat_out


def wrap_outputs_maintaining_identity(outputs, unwrapped_inputs, orig_inputs, level):
    flat_output, _ = pytree.tree_flatten(outputs)
    flat_unwrapped_inputs, _ = pytree.tree_flatten(unwrapped_inputs)
    flat_orig_inputs, _ = pytree.tree_flatten(orig_inputs)

    unwrapped_input_to_orig_input = {
        id(unwrapped): orig
        for unwrapped, orig in zip(flat_unwrapped_inputs, flat_orig_inputs)
    }

    flat_outputs, spec = pytree.tree_flatten(outputs)
    result = []

    for output in flat_outputs:
        if not isinstance(output, torch.Tensor):
            result.append(output)
            continue
        if id(output) in unwrapped_input_to_orig_input:
            result.append(unwrapped_input_to_orig_input[id(output)])
            continue
        result.append(_wrap_for_grad(output, level))

    return pytree.tree_unflatten(result, spec)


# NOTE: [functorch vjp and autograd interaction]
# There's an edge case with the functorch vjp and autograd interaction
# that will eventually be fixed by mode-only functorch.
# The TL;DR is that there's no way to unwrap a dead GradTensorWrapper,
# so we (the framework) need to do it manually. Regular PyTorch operators
# automatically do so this is consisent.
#
# class MyExp(torch.autograd.Function):
#     @staticmethod
#     def forward(x):
#         return x.exp()
#
#     @staticmethod
#     def setup_context(ctx, outputs, x):
#         y = outputs
#         ctx.save_for_backward(y)
#
#     @staticmethod
#     def backward(gy):
#         y, = ctx.saved_tensors()
#         return MyMul.apply(gy, y)
#
# x = torch.randn([], requires_grad=True)
# gy = torch.randn([], requires_grad=True)
# _, vjp_fn = vjp(MySin.apply, x)
# result = vjp_fn(gy)
#
# MyMul is an autograd.Function that is not shown here.
# It saves a `y` for backward (since gy requires grad).
#
# in vjp_fn(gy), we get:
# > MyMul.apply(gy, GradTensorWrapper(y, level=dead))
# Because the y that is saved for backward by MyExp is a GradTensorWrapper
# but is now dead since we are outside the vjp context.
#
# PyTorch dispatcher operations, upon seeing a dead GradTensorWrapper,
# will automatically unwrap the GradTensorWrapper when applied.
# But since autograd.Function technically sits above the regular PyTorch
# dispatcher, it doesn't get this treatment. So we manually do
# the unwrapping to be consistent with regular PyTorch dispatcher operations.


class VmapInfo(NamedTuple):
    batch_size: int


@custom_function_call.py_impl(TransformType.Vmap)
def custom_function_call_vmap(interpreter, autograd_function, *operands):
    if not hasattr(autograd_function, "vmap"):
        # TODO: link docs when they're ready.
        raise RuntimeError(
            f"You tried to vmap over {autograd_function.__name__}, but "
            f"it does not have a vmap rule defined. Please add a vmap "
            f"staticmethod to it.")

    current_level = interpreter.level()
    info = VmapInfo(batch_size=interpreter.batch_size())
    unwrapped_operands, in_dims = unwrap_batched(operands, current_level)

    # If none of the tensors are batched at the current level, then we skip the
    # current level. This saves the user from needing to handle this case in
    # their vmap staticmethod (and is consistent with our C++ batching rule API)
    if pytree.tree_all(lambda dim: dim is None, in_dims):
        with interpreter.lower():
            return custom_function_call(autograd_function, *operands)

    with interpreter.lower():
        unwrapped_output, out_dims = autograd_function.vmap(info, in_dims, *unwrapped_operands)

    output = wrap_batched(unwrapped_output, out_dims, current_level)
    # ctx.mark_dirty expects outputs that are the same as inputs to have
    # the same object identity.
    return preserve_object_identity(output, operands, unwrapped_output, unwrapped_operands)


def unwrap_batched(args, level):
    flat_args, spec = pytree.tree_flatten(args)
    if len(flat_args) == 0:
        return args, ()
    result = [_unwrap_batched(arg, level) if isinstance(arg, torch.Tensor)
              else (arg, None) for arg in flat_args]
    output, bdims = zip(*result)
    return pytree.tree_unflatten(output, spec), pytree.tree_unflatten(bdims, spec)


def wrap_batched(args, bdims, level):
    # TODO: raise better error message to the user when they don't follow the API.
    # Should probably mimic the logic of _process_batched_inputs,
    # but that one is hyperspecialized on error messages.
    flat_args, spec = pytree.tree_flatten(args)
    flat_bdims = _broadcast_to_and_flatten(bdims, spec)
    assert flat_bdims is not None
    result = _create_batched_inputs(flat_bdims, flat_args, level, spec)
    return result


def preserve_object_identity(outputs, inputs, unwrapped_outputs, unwrapped_inputs):
    flat_outputs, _ = pytree.tree_flatten(outputs)
    flat_unwrapped_inputs, _ = pytree.tree_flatten(unwrapped_inputs)
    flat_inputs, _ = pytree.tree_flatten(inputs)

    unwrapped_input_to_input = {
        id(unwrapped): inp
        for unwrapped, inp in zip(flat_unwrapped_inputs, flat_inputs)
    }

    flat_unwrapped_outputs, spec = pytree.tree_flatten(unwrapped_outputs)
    result = []

    for output, unwrapped_output in zip(flat_outputs, flat_unwrapped_outputs):
        if not isinstance(output, torch.Tensor):
            result.append(output)
            continue
        if id(unwrapped_output) in unwrapped_input_to_input:
            result.append(unwrapped_input_to_input[id(unwrapped_output)])
            continue
        result.append(output)

    return pytree.tree_unflatten(result, spec)


@custom_function_call.py_impl(TransformType.Functionalize)
def custom_function_call_functionalize(interpreter, autograd_function, *operands):
    raise RuntimeError("NYI: Functionalize rule for custom_function_call")

import logging
from typing import Tuple

import torch
from torch.nn.utils.prune import _validate_pruning_amount_init, _compute_nparams_toprune, \
    _validate_pruning_amount
from abc import ABC, abstractmethod
from collections.abc import Iterable

class BinaryStep(torch.autograd.Function):
    @staticmethod
    def forward(ctx, input):
        ctx.save_for_backward(input)
        return (input>0.).float()

    @staticmethod
    def backward(ctx, grad_output):
        input, = ctx.saved_tensors
        grad_input = grad_output.clone()
        zero_index = torch.abs(input) > 1
        middle_index = (torch.abs(input) <= 1) * (torch.abs(input) > 0.4)
        additional = 2-4*torch.abs(input)
        additional[zero_index] = 0.
        additional[middle_index] = 0.4
        return grad_input*additional

class BasePruningMethod(ABC):
    r"""Abstract base class for creation of new pruning techniques.
    Provides a skeleton for customization requiring the overriding of methods
    such as :meth:`compute_mask` and :meth:`apply`.
    """
    _tensor_name: str

    def __init__(self):
        pass

    def __call__(self, module, inputs):
        r"""Multiplies the mask (stored in ``module[name + '_mask']``)
        into the original tensor (stored in ``module[name + '_orig']``)
        and stores the result into ``module[name]`` by using
        :meth:`apply_mask`.
        Args:
            module (nn.Module): module containing the tensor to prune
            inputs: not used.
        """
        setattr(module, self._tensor_name, self.apply_mask(module))

    @abstractmethod
    def compute_mask(self, t, default_mask):
        r"""Computes and returns a mask for the input tensor ``t``.
        Starting from a base ``default_mask`` (which should be a mask of ones
        if the tensor has not been pruned yet), generate a random mask to
        apply on top of the ``default_mask`` according to the specific pruning
        method recipe.
        Args:
            t (torch.Tensor): tensor representing the importance scores of the
            parameter to prune.
            default_mask (torch.Tensor): Base mask from previous pruning
            iterations, that need to be respected after the new mask is
            applied. Same dims as ``t``.
        Returns:
            mask (torch.Tensor): mask to apply to ``t``, of same dims as ``t``
        """
        pass

    def apply_mask(self, module):
        r"""Simply handles the multiplication between the parameter being
        pruned and the generated mask.
        Fetches the mask and the original tensor from the module
        and returns the pruned version of the tensor.
        Args:
            module (nn.Module): module containing the tensor to prune
        Returns:
            pruned_tensor (torch.Tensor): pruned version of the input tensor
        """
        # to carry out the multiplication, the mask needs to have been computed,
        # so the pruning method must know what tensor it's operating on
        assert self._tensor_name is not None, "Module {} has to be pruned".format(
            module
        )  # this gets set in apply()
        mask = getattr(module, self._tensor_name + "_mask")
        orig = getattr(module, self._tensor_name + "_orig")
        pruned_tensor = mask.to(dtype=orig.dtype) * orig
        return pruned_tensor

    @classmethod
    def apply(cls, module, name, *args, importance_scores=None, **kwargs):
        r"""Adds the forward pre-hook that enables pruning on the fly and
        the reparametrization of a tensor in terms of the original tensor
        and the pruning mask.
        Args:
            module (nn.Module): module containing the tensor to prune
            name (str): parameter name within ``module`` on which pruning
                will act.
            args: arguments passed on to a subclass of
                :class:`BasePruningMethod`
            importance_scores (torch.Tensor): tensor of importance scores (of
                same shape as module parameter) used to compute mask for pruning.
                The values in this tensor indicate the importance of the
                corresponding elements in the parameter being pruned.
                If unspecified or None, the parameter will be used in its place.
            kwargs: keyword arguments passed on to a subclass of a
                :class:`BasePruningMethod`
        """

        def _get_composite_method(cls, module, name, *args, **kwargs):
            # Check if a pruning method has already been applied to
            # `module[name]`. If so, store that in `old_method`.
            old_method = None
            found = 0
            # there should technically be only 1 hook with hook.name == name
            # assert this using `found`
            hooks_to_remove = []
            for k, hook in module._forward_pre_hooks.items():
                # if it exists, take existing thing, remove hook, then
                # go through normal thing
                if isinstance(hook, BasePruningMethod) and hook._tensor_name == name:
                    old_method = hook
                    hooks_to_remove.append(k)
                    found += 1
            assert (
                found <= 1
            ), "Avoid adding multiple pruning hooks to the\
                same tensor {} of module {}. Use a PruningContainer.".format(
                name, module
            )

            for k in hooks_to_remove:
                del module._forward_pre_hooks[k]

            # Apply the new pruning method, either from scratch or on top of
            # the previous one.
            method = cls(*args, **kwargs)  # new pruning
            # Have the pruning method remember what tensor it's been applied to
            method._tensor_name = name

            # combine `methods` with `old_method`, if `old_method` exists
            if old_method is not None:  # meaning that there was a hook
                # if the hook is already a pruning container, just add the
                # new pruning method to the container
                if isinstance(old_method, PruningContainer):
                    old_method.add_pruning_method(method)
                    method = old_method  # rename old_method --> method

                # if the hook is simply a single pruning method, create a
                # container, add the old pruning method and the new one
                elif isinstance(old_method, BasePruningMethod):
                    container = PruningContainer(old_method)
                    # Have the pruning method remember the name of its tensor
                    # setattr(container, '_tensor_name', name)
                    container.add_pruning_method(method)
                    method = container  # rename container --> method
            return method

        method = _get_composite_method(cls, module, name, *args, **kwargs)
        # at this point we have no forward_pre_hooks but we could have an
        # active reparametrization of the tensor if another pruning method
        # had been applied (in which case `method` would be a PruningContainer
        # and not a simple pruning method).

        # Pruning is to be applied to the module's tensor named `name`,
        # starting from the state it is found in prior to this iteration of
        # pruning. The pruning mask is calculated based on importances scores.

        orig = getattr(module, name)
        if importance_scores is not None:
            assert (
                importance_scores.shape == orig.shape
            ), "importance_scores should have the same shape as parameter \
                {} of {}".format(
                name, module
            )
        else:
            importance_scores = orig

        # If this is the first time pruning is applied, take care of moving
        # the original tensor to a new parameter called name + '_orig' and
        # and deleting the original parameter
        if not isinstance(method, PruningContainer):
            # copy `module[name]` to `module[name + '_orig']`
            module.register_parameter(name + "_orig", orig)
            # temporarily delete `module[name]`
            del module._parameters[name]
            default_mask = torch.ones_like(orig)  # temp
        # If this is not the first time pruning is applied, all of the above
        # has been done before in a previous pruning iteration, so we're good
        # to go
        else:
            default_mask = (
                getattr(module, name + "_mask")
                .detach()
                .clone(memory_format=torch.contiguous_format)
            )

        # Use try/except because if anything goes wrong with the mask
        # computation etc., you'd want to roll back.
        try:
            # get the final mask, computed according to the specific method
            mask = method.compute_mask(importance_scores, default_mask=default_mask)
            # reparameterize by saving mask to `module[name + '_mask']`...
            module.register_buffer(name + "_mask", mask)
            # ... and the new pruned tensor to `module[name]`
            setattr(module, name, method.apply_mask(module))
            # associate the pruning method to the module via a hook to
            # compute the function before every forward() (compile by run)
            module.register_forward_pre_hook(method)

        except Exception as e:
            if not isinstance(method, PruningContainer):
                orig = getattr(module, name + "_orig")
                module.register_parameter(name, orig)
                del module._parameters[name + "_orig"]
            raise e

        return method

    def prune(self, t, default_mask=None, importance_scores=None):
        r"""Computes and returns a pruned version of input tensor ``t``
        according to the pruning rule specified in :meth:`compute_mask`.
        Args:
            t (torch.Tensor): tensor to prune (of same dimensions as
                ``default_mask``).
            importance_scores (torch.Tensor): tensor of importance scores (of
                same shape as ``t``) used to compute mask for pruning ``t``.
                The values in this tensor indicate the importance of the
                corresponding elements in the ``t`` that is being pruned.
                If unspecified or None, the tensor ``t`` will be used in its place.
            default_mask (torch.Tensor, optional): mask from previous pruning
                iteration, if any. To be considered when determining what
                portion of the tensor that pruning should act on. If None,
                default to a mask of ones.
        Returns:
            pruned version of tensor ``t``.
        """
        if importance_scores is not None:
            assert (
                importance_scores.shape == t.shape
            ), "importance_scores should have the same shape as tensor t"
        else:
            importance_scores = t
        default_mask = default_mask if default_mask is not None else torch.ones_like(t)
        return t * self.compute_mask(importance_scores, default_mask=default_mask)

    def remove(self, module):
        r"""Removes the pruning reparameterization from a module. The pruned
        parameter named ``name`` remains permanently pruned, and the parameter
        named ``name+'_orig'`` is removed from the parameter list. Similarly,
        the buffer named ``name+'_mask'`` is removed from the buffers.
        Note:
            Pruning itself is NOT undone or reversed!
        """
        # before removing pruning from a tensor, it has to have been applied
        assert (
            self._tensor_name is not None
        ), "Module {} has to be pruned\
            before pruning can be removed".format(
            module
        )  # this gets set in apply()

        # to update module[name] to latest trained weights
        weight = self.apply_mask(module)  # masked weights

        # delete and reset
        if hasattr(module, self._tensor_name):
            delattr(module, self._tensor_name)
        orig = module._parameters[self._tensor_name + "_orig"]
        orig.data = weight.data
        del module._parameters[self._tensor_name + "_orig"]
        del module._buffers[self._tensor_name + "_mask"]
        setattr(module, self._tensor_name, orig)

class PruningContainer(BasePruningMethod):
    """Container holding a sequence of pruning methods for iterative pruning.
    Keeps track of the order in which pruning methods are applied and handles
    combining successive pruning calls.
    Accepts as argument an instance of a BasePruningMethod or an iterable of
    them.
    """

    def __init__(self, *args):
        self._pruning_methods: Tuple["BasePruningMethod", ...] = tuple()
        if not isinstance(args, Iterable):  # only 1 item
            self._tensor_name = args._tensor_name
            self.add_pruning_method(args)
        elif len(args) == 1:  # only 1 item in a tuple
            self._tensor_name = args[0]._tensor_name
            self.add_pruning_method(args[0])
        else:  # manual construction from list or other iterable (or no args)
            for method in args:
                self.add_pruning_method(method)

    def add_pruning_method(self, method):
        r"""Adds a child pruning ``method`` to the container.
        Args:
            method (subclass of BasePruningMethod): child pruning method
                to be added to the container.
        """
        # check that we're adding a pruning method to the container
        if not isinstance(method, BasePruningMethod) and method is not None:
            raise TypeError(
                "{} is not a BasePruningMethod subclass".format(type(method))
            )
        elif method is not None and self._tensor_name != method._tensor_name:
            raise ValueError(
                "Can only add pruning methods acting on "
                "the parameter named '{}' to PruningContainer {}.".format(
                    self._tensor_name, self
                )
                + " Found '{}'".format(method._tensor_name)
            )
        # if all checks passed, add to _pruning_methods tuple
        self._pruning_methods += (method,)  # type: ignore[operator]

    def __len__(self):
        return len(self._pruning_methods)

    def __iter__(self):
        return iter(self._pruning_methods)

    def __getitem__(self, idx):
        return self._pruning_methods[idx]

    def compute_mask(self, t, default_mask):
        r"""Applies the latest ``method`` by computing the new partial masks
        and returning its combination with the ``default_mask``.
        The new partial mask should be computed on the entries or channels
        that were not zeroed out by the ``default_mask``.
        Which portions of the tensor ``t`` the new mask will be calculated from
        depends on the ``PRUNING_TYPE`` (handled by the type handler):
        * for 'unstructured', the mask will be computed from the raveled
          list of nonmasked entries;
        * for 'structured', the mask will be computed from the nonmasked
          channels in the tensor;
        * for 'global', the mask will be computed across all entries.
        Args:
            t (torch.Tensor): tensor representing the parameter to prune
                (of same dimensions as ``default_mask``).
            default_mask (torch.Tensor): mask from previous pruning iteration.
        Returns:
            mask (torch.Tensor): new mask that combines the effects
            of the ``default_mask`` and the new mask from the current
            pruning ``method`` (of same dimensions as ``default_mask`` and
            ``t``).
        """

        def _combine_masks(method, t, mask):
            r"""
            Args:
                method (a BasePruningMethod subclass): pruning method
                    currently being applied.
                t (torch.Tensor): tensor representing the parameter to prune
                    (of same dimensions as mask).
                mask (torch.Tensor): mask from previous pruning iteration
            Returns:
                new_mask (torch.Tensor): new mask that combines the effects
                    of the old mask and the new mask from the current
                    pruning method (of same dimensions as mask and t).
            """
            new_mask = mask  # start off from existing mask
            new_mask = new_mask.to(dtype=t.dtype)

            # compute a slice of t onto which the new pruning method will operate
            if method.PRUNING_TYPE == "unstructured":
                # prune entries of t where the mask is 1
                slc = mask == 1

            # for struct pruning, exclude channels that have already been
            # entirely pruned
            elif method.PRUNING_TYPE == "structured":
                if not hasattr(method, "dim"):
                    raise AttributeError(
                        "Pruning methods of PRUNING_TYPE "
                        '"structured" need to have the attribute `dim` defined.'
                    )

                # find the channels to keep by removing the ones that have been
                # zeroed out already (i.e. where sum(entries) == 0)
                n_dims = t.dim()  # "is this a 2D tensor? 3D? ..."
                dim = method.dim
                # convert negative indexing
                if dim < 0:
                    dim = n_dims + dim
                # if dim is still negative after subtracting it from n_dims
                if dim < 0:
                    raise IndexError(
                        "Index is out of bounds for tensor with dimensions {}".format(
                            n_dims
                        )
                    )
                # find channels along dim = dim that aren't already tots 0ed out
                keep_channel = mask.sum(dim=[d for d in range(n_dims) if d != dim]) != 0
                # create slice to identify what to prune
                slc = [slice(None)] * n_dims
                slc[dim] = keep_channel

            elif method.PRUNING_TYPE == "global":
                n_dims = len(t.shape)  # "is this a 2D tensor? 3D? ..."
                slc = [slice(None)] * n_dims

            else:
                raise ValueError(
                    "Unrecognized PRUNING_TYPE {}".format(method.PRUNING_TYPE)
                )

            # compute the new mask on the unpruned slice of the tensor t
            # partial_mask = method.compute_mask(t[slc], default_mask=mask[slc])
            # new_mask[slc] = partial_mask.to(dtype=new_mask.dtype)

            partial_mask = method.compute_mask(t, default_mask=mask)
            new_mask[slc] = partial_mask[slc].to(dtype=new_mask.dtype)

            return new_mask

        method = self._pruning_methods[-1]
        mask = _combine_masks(method, t, default_mask)
        return mask

class CustomFromMask(BasePruningMethod):

    PRUNING_TYPE = "global"

    def __init__(self, mask):
        self.mask = mask

    def compute_mask(self, t, default_mask):
        assert default_mask.shape == self.mask.shape
        mask = default_mask * self.mask.to(dtype=default_mask.dtype)
        return mask

    @classmethod
    def apply(cls, module, name, mask):
        r"""Adds the forward pre-hook that enables pruning on the fly and
        the reparametrization of a tensor in terms of the original tensor
        and the pruning mask.
        Args:
            module (nn.Module): module containing the tensor to prune
            name (str): parameter name within ``module`` on which pruning
                will act.
        """
        return super(CustomFromMask, cls).apply(module, name, mask=mask)

class Unstructured(BasePruningMethod):

    PRUNING_TYPE = "unstructured"

    def __init__(self, amount, mode=0):
        # Check range of validity of pruning amount
        _validate_pruning_amount_init(amount)
        self.mode = mode
        self.amount = amount
        self.step = BinaryStep.apply

    def compute_mask(self, t, default_mask):
        mask = default_mask.clone(memory_format=torch.contiguous_format)
        # logging.info("mask: "+str(mask))
        # logging.info("mask shape: " + str(mask.shape))
        # ratio mode
        if self.mode==0:
            # Check that the amount of units to prune is not > than the number of
            # parameters in t
            tensor_size = t.nelement()
            # Compute number of units to prune: amount if int,
            # else amount * tensor_size
            nparams_toprune = _compute_nparams_toprune(self.amount, tensor_size)
            # This should raise an error if the number of units to prune is larger
            # than the number of units in the tensor
            _validate_pruning_amount(nparams_toprune, tensor_size)

            if nparams_toprune != 0:  # k=0 not supported by torch.kthvalue
                logging.info("Mode " + str(self.mode) + ", pruning")
                # largest=True --> top k; largest=False --> bottom k
                # Prune the smallest k
                topk = torch.topk(torch.abs(t).view(-1), k=nparams_toprune, largest=False)
                # topk will have .indices and .values
                indices = topk.indices
                mask.view(-1)[indices] = 0

        # threshold mode
        else:
            # indices = (torch.abs(t).view(-1)<self.amount).nonzero()
            # mask.view(-1)[indices] = 0
            slc = mask==1
            new_mask = self.step(torch.abs(t)-self.amount)
            mask[slc] = new_mask[slc]

        num = default_mask.view(-1).sum() - mask.view(-1).sum()
        logging.info("Mode "+str(self.mode)+", prune num: "+str(num))

        return mask

    @classmethod
    def apply(cls, module, name, amount, mode=0, importance_scores=None):

        return super(Unstructured, cls).apply(
            module, name, amount=amount, mode=mode, importance_scores=importance_scores
        )

def unstructured(module, name, amount, mode=0, importance_scores=None):
    r"""Prunes tensor corresponding to parameter called ``name`` in ``module``
    by removing the specified `amount` of (currently unpruned) units with the
    lowest L1-norm.
    Modifies module in place (and also return the modified module)
    by:
    1) adding a named buffer called ``name+'_mask'`` corresponding to the
       binary mask applied to the parameter ``name`` by the pruning method.
    2) replacing the parameter ``name`` by its pruned version, while the
       original (unpruned) parameter is stored in a new parameter named
       ``name+'_orig'``.
    Args:
        module (nn.Module): module containing the tensor to prune
        name (str): parameter name within ``module`` on which pruning
                will act.
        amount (int or float): quantity of parameters to prune.
            If ``float``, should be between 0.0 and 1.0 and represent the
            fraction of parameters to prune. If ``int``, it represents the
            absolute number of parameters to prune.
        importance_scores (torch.Tensor): tensor of importance scores (of same
            shape as module parameter) used to compute mask for pruning.
            The values in this tensor indicate the importance of the corresponding
            elements in the parameter being pruned.
            If unspecified or None, the module parameter will be used in its place.
    Returns:
        module (nn.Module): modified (i.e. pruned) version of the input module
    """
    Unstructured.apply(
        module, name, amount=amount, mode=mode, importance_scores=importance_scores
    )
    return module

def global_unstructured(parameters, pruning_method, importance_scores=None, **kwargs):
    r"""
    Globally prunes tensors corresponding to all parameters in ``parameters``
    by applying the specified ``pruning_method``.
    Modifies modules in place by:
    1) adding a named buffer called ``name+'_mask'`` corresponding to the
       binary mask applied to the parameter ``name`` by the pruning method.
    2) replacing the parameter ``name`` by its pruned version, while the
       original (unpruned) parameter is stored in a new parameter named
       ``name+'_orig'``.
    Args:
        parameters (Iterable of (module, name) tuples): parameters of
            the model to prune in a global fashion, i.e. by aggregating all
            weights prior to deciding which ones to prune. module must be of
            type :class:`nn.Module`, and name must be a string.
        pruning_method (function): a valid pruning function from this module,
            or a custom one implemented by the user that satisfies the
            implementation guidelines and has ``PRUNING_TYPE='unstructured'``.
        importance_scores (dict): a dictionary mapping (module, name) tuples to
            the corresponding parameter's importance scores tensor. The tensor
            should be the same shape as the parameter, and is used for computing
            mask for pruning.
            If unspecified or None, the parameter will be used in place of its
            importance scores.
        kwargs: other keyword arguments such as:
            amount (int or float): quantity of parameters to prune across the
            specified parameters.
            If ``float``, should be between 0.0 and 1.0 and represent the
            fraction of parameters to prune. If ``int``, it represents the
            absolute number of parameters to prune.
    Raises:
        TypeError: if ``PRUNING_TYPE != 'unstructured'``
    Note:
        Since global structured pruning doesn't make much sense unless the
        norm is normalized by the size of the parameter, we now limit the
        scope of global pruning to unstructured methods.
    """
    # ensure parameters is a list or generator of tuples
    if not isinstance(parameters, Iterable):
        raise TypeError("global_unstructured(): parameters is not an Iterable")

    importance_scores = importance_scores if importance_scores is not None else {}
    if not isinstance(importance_scores, dict):
        raise TypeError("global_unstructured(): importance_scores must be of type dict")

    # flatten importance scores to consider them all at once in global pruning
    relevant_importance_scores = torch.nn.utils.parameters_to_vector(
        [
            importance_scores.get((module, name), getattr(module, name))
            for (module, name) in parameters
        ]
    )
    # similarly, flatten the masks (if they exist), or use a flattened vector
    # of 1s of the same dimensions as t
    default_mask = torch.nn.utils.parameters_to_vector(
        [
            getattr(module, name + "_mask", torch.ones_like(getattr(module, name)))
            for (module, name) in parameters
        ]
    )

    # use the canonical pruning methods to compute the new mask, even if the
    # parameter is now a flattened out version of `parameters`
    container = PruningContainer()
    container._tensor_name = "temp"  # to make it match that of `method`
    method = pruning_method(**kwargs)
    method._tensor_name = "temp"  # to make it match that of `container`
    if method.PRUNING_TYPE != "unstructured":
        raise TypeError(
            'Only "unstructured" PRUNING_TYPE supported for '
            "the `pruning_method`. Found method {} of type {}".format(
                pruning_method, method.PRUNING_TYPE
            )
        )

    container.add_pruning_method(method)

    # use the `compute_mask` method from `PruningContainer` to combine the
    # mask computed by the new method with the pre-existing mask
    final_mask = container.compute_mask(relevant_importance_scores, default_mask)

    # Pointer for slicing the mask to match the shape of each parameter
    pointer = 0
    for module, name in parameters:

        param = getattr(module, name)
        # The length of the parameter
        num_param = param.numel()
        # Slice the mask, reshape it
        param_mask = final_mask[pointer : pointer + num_param].view_as(param)
        # Assign the correct pre-computed mask to each parameter and add it
        # to the forward_pre_hooks like any other pruning method
        custom_from_mask(module, name, mask=param_mask)

        # Increment the pointer to continue slicing the final_mask
        pointer += num_param

def custom_from_mask(module, name, mask):
    r"""Prunes tensor corresponding to parameter called ``name`` in ``module``
    by applying the pre-computed mask in ``mask``.
    Modifies module in place (and also return the modified module)
    by:
    1) adding a named buffer called ``name+'_mask'`` corresponding to the
       binary mask applied to the parameter ``name`` by the pruning method.
    2) replacing the parameter ``name`` by its pruned version, while the
       original (unpruned) parameter is stored in a new parameter named
       ``name+'_orig'``.
    Args:
        module (nn.Module): module containing the tensor to prune
        name (str): parameter name within ``module`` on which pruning
            will act.
        mask (Tensor): binary mask to be applied to the parameter.
    Returns:
        module (nn.Module): modified (i.e. pruned) version of the input module
    """
    CustomFromMask.apply(module, name, mask)
    return module

# class Structured(BasePruningMethod):
#     r"""Prune entire (currently unpruned) channels in a tensor based on their
#     L\ ``n``-norm.
#     Args:
#         amount (int or float): quantity of channels to prune.
#             If ``float``, should be between 0.0 and 1.0 and represent the
#             fraction of parameters to prune. If ``int``, it represents the
#             absolute number of parameters to prune.
#         n (int, float, inf, -inf, 'fro', 'nuc'): See documentation of valid
#             entries for argument ``p`` in :func:`torch.norm`.
#         dim (int, optional): index of the dim along which we define
#             channels to prune. Default: -1.
#     """
#
#     PRUNING_TYPE = "structured"
#
#     def __init__(self, amount, bn, dim=0):
#         # Check range of validity of amount
#         _validate_pruning_amount_init(amount)
#         self.amount = amount
#         self.dim = dim
#         self.bn = bn
#
#     def compute_mask(self, t, default_mask):
#         r"""Computes and returns a mask for the input tensor ``t``.
#         Starting from a base ``default_mask`` (which should be a mask of ones
#         if the tensor has not been pruned yet), generate a mask to apply on
#         top of the ``default_mask`` by zeroing out the channels along the
#         specified dim with the lowest L\ ``n``-norm.
#         Args:
#             t (torch.Tensor): tensor representing the parameter to prune
#             default_mask (torch.Tensor): Base mask from previous pruning
#                 iterations, that need to be respected after the new mask is
#                 applied.  Same dims as ``t``.
#         Returns:
#             mask (torch.Tensor): mask to apply to ``t``, of same dims as ``t``
#         Raises:
#             IndexError: if ``self.dim >= len(t.shape)``
#         """
#
#         indices = (torch.abs(self.bn)>self.amount).nonzero()
#
#         # Compute binary mask by initializing it to all 0s and then filling in
#         # 1s wherever topk.indices indicates, along self.dim.
#         # mask has the same shape as tensor t
#         def make_mask(t, dim, indices):
#             # init mask to 0
#             mask = torch.zeros_like(t)
#             # e.g.: slc = [None, None, None], if len(t.shape) = 3
#             slc = [slice(None)] * len(t.shape)
#             # replace a None at position=dim with indices
#             # e.g.: slc = [None, None, [0, 2, 3]] if dim=2 & indices=[0,2,3]
#             slc[dim] = indices
#             # use slc to slice mask and replace all its entries with 1s
#             # e.g.: mask[:, :, [0, 2, 3]] = 1
#             mask[slc] = 1
#             return mask
#
#         mask = make_mask(t, self.dim, indices)
#         mask *= default_mask.to(dtype=mask.dtype)
#
#         return mask
#
#     @classmethod
#     def apply(cls, module, name, amount, bn, dim=0, importance_scores=None):
#         r"""Adds the forward pre-hook that enables pruning on the fly and
#         the reparametrization of a tensor in terms of the original tensor
#         and the pruning mask.
#         Args:
#             module (nn.Module): module containing the tensor to prune
#             name (str): parameter name within ``module`` on which pruning
#                 will act.
#             amount (int or float): quantity of parameters to prune.
#                 If ``float``, should be between 0.0 and 1.0 and represent the
#                 fraction of parameters to prune. If ``int``, it represents the
#                 absolute number of parameters to prune.
#             n (int, float, inf, -inf, 'fro', 'nuc'): See documentation of valid
#                 entries for argument ``p`` in :func:`torch.norm`.
#             dim (int): index of the dim along which we define channels to
#                 prune.
#             importance_scores (torch.Tensor): tensor of importance scores (of same
#                 shape as module parameter) used to compute mask for pruning.
#                 The values in this tensor indicate the importance of the corresponding
#                 elements in the parameter being pruned.
#                 If unspecified or None, the module parameter will be used in its place.
#         """
#         return super(Structured, cls).apply(
#             module,
#             name,
#             amount=amount,
#             dim=dim,
#             bn=bn,
#             importance_scores=importance_scores,
#         )

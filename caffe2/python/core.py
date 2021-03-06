from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

from collections import namedtuple
from collections import OrderedDict

from caffe2.proto import caffe2_pb2
from collections import defaultdict
from caffe2.python import scope, utils, workspace, extension_loader

import caffe2.python._import_c_extension as C

GlobalInit = C.global_init

# Convenience redirections to functions inside scope.
DeviceScope = scope.DeviceScope
NameScope = scope.NameScope


# Bring datatype enums to the main namespace
class DataType:
    pass


def _InitDataType():
    for name, value in caffe2_pb2.TensorProto.DataType.items():
        setattr(DataType, name, value)

_InitDataType()

# Python 2 and 3 compatibility: test if basestring exists
try:
    basestring = basestring  # NOQA
except NameError:
    # This is python3 so we define basestring.
    basestring = str


def _GetRegisteredOperators():
    return set(s.decode() for s in workspace.RegisteredOperators())

_REGISTERED_OPERATORS = _GetRegisteredOperators()


def RefreshRegisteredOperators():
    global _REGISTERED_OPERATORS
    _REGISTERED_OPERATORS = _GetRegisteredOperators()


def IsOperator(op_type):
    return (op_type in _REGISTERED_OPERATORS)


def IsOperatorWithEngine(op_type, engine):
    return (op_type + "_ENGINE_" + engine in _REGISTERED_OPERATORS)


def DeviceOption(device_type, cuda_gpu_id=0, random_seed=None):
    option = caffe2_pb2.DeviceOption()
    option.device_type = device_type
    option.cuda_gpu_id = cuda_gpu_id
    if random_seed is not None:
        option.random_seed = random_seed
    return option


GradientSlice = namedtuple('GradientSlice', ['indices', 'values'])


class BlobReference(object):
    """A wrapper around a blob in a net.

    BlobReference gives us a way to refer to the network that the blob is
    generated from. Note that blobs are, essentially, just strings in the
    current workspace.
    """

    def __init__(self, name, net=None):
        """Initializes a blob reference.

        Note that this does not prepends the namescope. If needed, use
        ScopedBlobReference() to prepend the existing namespace.
        """
        self._name = name
        self._from_net = net
        # meta allows helper functions to put whatever metainformation needed
        # there.
        self.meta = {}

    def __hash__(self):
        return hash(self._name)

    def __eq__(self, other):
        if isinstance(other, basestring):
            return self._name == other
        elif isinstance(other, BlobReference):
            return self._name == other._name
        else:
            return False

    def __ne__(self, other):
        return not(self == other)

    def __str__(self):
        return self._name

    def __repr__(self):
        return 'BlobReference("{}")'.format(self._name)

    def __add__(self, other):
        if not isinstance(other, basestring):
            raise RuntimeError('Cannot add BlobReference to a non-string.')
        return BlobReference(self._name + other, self._from_net)

    def __radd__(self, other):
        if not isinstance(other, basestring):
            raise RuntimeError('Cannot add a non-string to BlobReference.')
        return BlobReference(other + self._name, self._from_net)

    def Net(self):
        return self._from_net

    def _CreateAndAddToNet(self, op_type, inputs=None, *args, **kwargs):
        """Internal function that routes the operator generation to the
        network's __getattr__ function.
        """
        inputs = [] if inputs is None else inputs
        if isinstance(inputs, BlobReference) or isinstance(inputs, str):
            inputs = [inputs]
        # add self to the input list.
        inputs.insert(0, self)
        return self._from_net.__getattr__(op_type)(inputs, *args, **kwargs)

    def __getattr__(self, op_type):
        """A wrapper allowing one to initiate operators from a blob reference.

        Example: for a blob reference b that comes from network n, doing
            b.Relu(...)
        is equivalent to doing
            net.Relu([b], ...)
        """
        if op_type.startswith('__'):
            raise AttributeError('Attribute {} not found.'.format(op_type))
        if self._from_net is None:
            raise RuntimeError(
                'You cannot use a blob reference that does not have a net '
                'source to create operators. Create the operator from an '
                'explicit net object.')
        if not IsOperator(op_type):
            raise RuntimeError(
                'Method ' + op_type + ' is not a registered operator.'
            )
        return lambda *args, **kwargs: self._CreateAndAddToNet(
            op_type, *args, **kwargs)


def ScopedBlobReference(name, *args, **kwargs):
    """Returns a blob reference with scope prefixed."""
    return BlobReference(scope.NAMESCOPE + name, *args, **kwargs)


def _RectifyInputOutput(blobs, net=None):
    """A helper function to rectify the input or output of the CreateOperator
    interface.
    """
    if isinstance(blobs, basestring):
        # If blobs is a single string, prepend scope.NAMESCOPE and put it as a
        # list.
        # TODO(jiayq): enforce using BlobReference instead of raw strings.
        return [ScopedBlobReference(blobs, net=net)]
    elif type(blobs) is BlobReference:
        # If blob is a BlobReference, simply put it as a list.
        return [blobs]
    elif type(blobs) in (list, tuple):
        # If blob is a list, we go through it and type check.
        rectified = []
        for blob in blobs:
            if isinstance(blob, basestring):
                rectified.append(ScopedBlobReference(blob, net=net))
            elif type(blob) is BlobReference:
                rectified.append(blob)
            else:
                raise TypeError(
                    "I/O blob #{} of unsupported type: {} of type {}"
                    .format(len(rectified), str(blob), type(blob)))
        return rectified
    else:
        raise TypeError(
            "Unknown input/output type: %s of type %s." %
            (str(blobs), type(blobs))
        )


def CreateOperator(
    operator_type,
    inputs,
    outputs,
    name='',
    control_input=None,
    device_option=None,
    arg=None,
    engine=None,
    **kwargs
):
    """A function wrapper that allows one to create operators based on the
    operator type. The type should be a string corresponding to an operator
    registered with Caffe2.
    """
    operator = caffe2_pb2.OperatorDef()
    operator.type = operator_type
    operator.name = name
    # Add rectified inputs and outputs
    inputs = _RectifyInputOutput(inputs)
    outputs = _RectifyInputOutput(outputs)
    operator.input.extend([str(i) for i in inputs])
    operator.output.extend([str(o) for o in outputs])
    if control_input:
        control_input = _RectifyInputOutput(control_input)
        operator.control_input.extend([str(i) for i in control_input])
    # Set device option:
    # (1) If device_option is explicitly set, use device_option.
    # (2) If not, but scope.DEVICESCOPE is set, then we use scope.DEVICESCOPE.
    # (3) Otherwise, do not set device option.
    if device_option is not None:
        operator.device_option.CopyFrom(device_option)
    elif scope.DEVICESCOPE is not None:
        operator.device_option.CopyFrom(scope.DEVICESCOPE)
    if engine is not None:
        operator.engine = engine
    # random seed is defined in the device option, so we need to do special
    # care.
    if 'random_seed' in kwargs:
        operator.device_option.random_seed = kwargs['random_seed']
        del kwargs['random_seed']
    # Add given arguments that do not need parsing
    if arg is not None:
        operator.arg.extend(arg)
    # Add all other arguments
    for key, value in kwargs.items():
        operator.arg.add().CopyFrom(utils.MakeArgument(key, value))

    if workspace.IsImmediate():
        workspace.RunOperatorImmediate(operator)
    return operator


def GetIndexFromGradientList(g_list, name):
    """A helper function to get the index from a gradient list, None if not
    matching."""
    for i, g in enumerate(g_list):
        if g == name:
            return i
        elif type(g) is GradientSlice:
            if (g.indices == name or g.values == name):
                return i
    return None


OpSSA = namedtuple('OpSSA', ['op', 'in_versions', 'out_versions'])
GradGenMeta = namedtuple('GradGenMeta', ['grad_op', 'idx', 'gradient'])


class IR(object):
    """A simple IR class to keep track of all intermediate representations used
    in the gradient computation.
    """

    def __init__(self, operators):
        # The IR class holds multiple metadata from the forward pass:
        # a) ssa: a list of [op, in_versions, out_versions] recording the
        #    input and the output version of each operator, similar
        #    to a normal SSA form.
        # b) input_count: a dictionary specifying for each blob and
        #    each of its version, how many times it is used as input for another
        #    op.
        # c) frontier: maintaining the current versions of the blobs
        #    we are having in the workspace, after the execution of all the ops
        #    added to the IR so far. This is useful because if a gradient is
        #    trying to access an earlier version of a blob, we can sanity check
        #    that it is no longer there, and thus throw an error.
        # d) gradient_frontier: maps the names of blobs to its version that the
        #    gradient corresponds to.
        # e) gradient_generators: for each blob and each of its version, maps to
        #    a list of operators that generates its gradient together with the
        #    gradient name.
        self.ssa = []
        self.input_usages = defaultdict(lambda: defaultdict(list))
        self.frontier = defaultdict(int)
        self.gradient_frontier = {}
        self.gradient_generators = defaultdict(lambda: defaultdict(list))

        for op in operators:
            self.Play(op)

    def Play(self, op):
        """"Adds an op to the current IR, and update the internal states to
        reflect the blobs and versions after the execution of the op.
        """
        # For input, they are the current version in the dict.
        in_versions = {}
        for s in op.input:
            in_versions[s] = self.frontier[s]
            self.input_usages[s][self.frontier[s]].append(len(self.ssa))
        # For output, they are the current version plus one. If this is a
        # newly created blob, its version starts with zero.
        out_versions = {}
        for s in op.output:
            if s in self.frontier:
                self.frontier[s] += 1
            out_versions[s] = self.frontier[s]
        # Add to SSA for bookkeeping.
        self.ssa.append(OpSSA(op, in_versions, out_versions))

    def CheckGradientOperators(  # NOQA
            self, fwd_op_idx, gradient_ops, g_output, g_input):
        """Checks if the gradient operators can be correctly carried out."""
        forward_op, in_versions, out_versions = self.ssa[fwd_op_idx]
        locally_generated_blobs = []

        for grad_op in gradient_ops:
            # (1) for inputs:
            # (1a) If it is a dense or sparse gradient name, it should match the
            #      version of the corresponding output.
            # (1b) If it is an output name, the current version should match the
            #      version when the operator was run.
            # (1c) If it is an input name, the current version should match the
            #      version when the operator was run.
            # (1d) If it is none of the above, it should be a blob that is
            #      generated locally by one of the previous gradient operators.
            for s in grad_op.input:  # (1)
                original_index = GetIndexFromGradientList(g_output, s)
                if original_index is not None:  # (1a)
                    original_name = forward_op.output[original_index]
                    if (out_versions[original_name] !=
                            self.gradient_frontier[original_name]):
                        raise RuntimeError(
                            'Gradient name "%s" is expected to correspond '
                            'to version %d of "%s", but currently we have '
                            'version %d.' % (
                                s, out_versions[original_name],
                                original_name,
                                self.gradient_frontier[original_name]))
                elif s in out_versions:  # (1b)
                    if self.frontier[s] != out_versions[s]:
                        raise RuntimeError(
                            'Gradient operator needs output "%s" at version'
                            ' %d, but currently we have version %d.' % (
                                s, out_versions[s],
                                self.frontier[s]
                            )
                        )
                elif s in in_versions:  # (1c)
                    if (self.frontier[s] != in_versions[s]):
                        raise RuntimeError(
                            'Gradient operator needs input "%s" at version '
                            '%d, but currently we have version %d.' % (
                                s, in_versions[s],
                                self.frontier[s]
                            )
                        )
                else:  # (1d)
                    if s not in locally_generated_blobs:
                        raise RuntimeError(
                            'Blob name "%s" not in the scope of operator: '
                            '%s\nand is not generated by any of the local '
                            'gradient operators.' % (s, str(forward_op))
                        )
            # (2) for outputs: we will simply add them to locally generated
            # blobs. We will also record the output to gradient_generators for
            # bookkeeping, if the output corresponds to the input of a gradient.
            for i, s in enumerate(grad_op.output):  # (1)
                locally_generated_blobs.extend(grad_op.output)
                input_index = GetIndexFromGradientList(g_input, s)
                if input_index is not None:
                    input_name = forward_op.input[input_index]
                    input_version = in_versions[input_name]
                    self.gradient_generators[input_name][input_version].append(
                        GradGenMeta(grad_op, i, g_input[input_index]))

        # (3) for ops (e.g., Add, Sum, Sub) which have grdient outputs directly
        # passed from inputs (not computed from gradient ops), we create an
        # GradGenMeta with None grad_op and idx so that the gradient_generators
        # knows where the gradients are coming from. This is needed for creating
        # Sum op to accumulate the gradients from multiple parents.
        for input_index, g in enumerate(g_input):
            if not g or str(g) in [str(b) for b in locally_generated_blobs]:
                continue
            input_name = forward_op.input[input_index]
            input_version = in_versions[input_name]
            self.gradient_generators[input_name][input_version].append(
                GradGenMeta(None, 0, g))

        # Finally, for the gradients specified in g_input, we update the
        # gradient frontier to reflect the input versions that the gradients
        # correspond to.
        for i, g in enumerate(g_input):
            if g is not None:
                input_name = forward_op.input[i]
                input_version = in_versions[input_name]
                self.gradient_frontier[input_name] = input_version

    def _GetSumOpOutputName(self, generator, input_name):
        sum_op_output = None
        for grad_op, idx, _ in generator:
            if grad_op and not sum_op_output:
                sum_op_output = grad_op.output[idx]
        return sum_op_output or input_name + '_grad'

    def _MakeSumOp(self, input_name, input_version):
        generator = self.gradient_generators[input_name][input_version]
        sum_op_input = []
        sum_op_output = self._GetSumOpOutputName(generator, input_name)
        current = 0
        for grad_op, idx, g in generator:
            if grad_op:
                grad_op.output[idx] = ('_' + grad_op.output[idx] +
                                       '_autosplit_{}'.format(current))
                sum_op_input.append(grad_op.output[idx])
                current += 1
            else:
                if str(sum_op_output) == str(g):
                    raise RuntimeError(
                        'The gradient output of empty gradient op can not '
                        'be the same as the normal name of the current '
                        'input gradient.')
                sum_op_input.append(g)

        sum_op = CreateOperator("Sum", sum_op_input, sum_op_output)
        for g in generator:
            if g.grad_op:
                if g.grad_op.HasField('device_option'):
                    sum_op.device_option.CopyFrom(g.grad_op.device_option)
                break

        return sum_op

    def _VerifyGradientGenerators(self, generator):
        # (1) check if we are dealing with dense gradients. Sparse gradients
        # do not support automatic aggregation yet.
        if any(type(g[2]) is GradientSlice for g in generator):
            raise RuntimeError(
                'Automatic gradient aggregation does not work with sparse '
                'gradients yet.')

        # If for all the operators that used the operator, none or only one
        # produced the gradient, then no additional sum needs to be carried
        # out.
        if len(generator) < 2:
            return False

        all_gradient_names = []
        all_device_options = []
        for g in generator:
            if g.grad_op:
                all_gradient_names.append(g.grad_op.output[g.idx])
                all_device_options.append(g.grad_op.device_option)
        # Check if all grad names are the same.
        if len(set(all_gradient_names)) > 1:
            raise RuntimeError('Unexpected behavior: not all grad output '
                               'names are the same.')
        # Check if all grad op device options are the same.
        if len(all_device_options) >= 2 and not all(
                d == all_device_options[0] for d in all_device_options[1:]):
            raise RuntimeError('Unexpected behavior: not all grad ops'
                               'have the same device option.')
        return True

    def DoGradientAccumulation(self, fwd_op_idx):
        """For each input name in the forward op, check if we will need to
        add gradient accumulation. If so, do gradient accumulation and return
        the list of gradient operators.

        The criteria for doing gradient accumulation is:
        (1) the specific input version has been used by multiple operators.
        (2) the current fwd_op_idx is the first to use that input, i.e. in the
            backward pass, is the last to optionally generate the gradient for
            the op.
        (3) For the operators that used the input, their gradient operators
            have generated more than 1 gradient.

        When accumulating operators, our current solution is to rename all the
        created gradients with an internal intermediate name, and then add a
        Sum() operator that adds up all the gradients. This may use more memory
        due to intermediate storage, but is usually the fastest approach as one
        can do one single sum for multiple intermediate gradients.
        """
        forward_op, in_versions, out_versions = self.ssa[fwd_op_idx]
        additional_sum_ops = []
        grad_map = {}
        for i, input_name in enumerate(set(forward_op.input)):
            input_version = in_versions[input_name]
            input_usage = self.input_usages[input_name][input_version]
            if (len(input_usage) <= 1 or fwd_op_idx != input_usage[0]):
                # We do not need to do gradient accumulation yet.
                continue
            generator = self.gradient_generators[input_name][input_version]
            try:
                if not self._VerifyGradientGenerators(generator):
                    continue
            except RuntimeError as err:
                raise RuntimeError(
                    "Gradients for param ''{}'' failed to verity: {}".format(
                        input_name,
                        err
                    )
                )

            # Finally, let's create the sum operator.
            sum_op = self._MakeSumOp(input_name, input_version)
            additional_sum_ops.append(sum_op)
            grad_map[input_name] = sum_op.output[0]
        return additional_sum_ops, grad_map

    def _GetInitGradients(self, ys):
        input_to_grad = {}
        gradient_ops = []
        for y, g in ys.items():
            if g is None:
                autograd_op = CreateOperator(
                    "ConstantFill", [y], [str(y) + "_autogen_grad"],
                    value=1.0)
                gradient_ops.append(autograd_op)
                g = autograd_op.output[0]
            # Since the C++ gradient registry does not have notion of
            # NameScopes, we will convert all references to strings.
            input_to_grad[str(y)] = (
                GradientSlice(str(g[0]), str(g[1]))
                if isinstance(g, GradientSlice) else str(g))

        return input_to_grad, gradient_ops

    def _GenerateGradientsForForwardOp(
            self, forward_op_idx, input_to_grad):
        new_input_to_grad = {}
        gradient_ops = []
        forward_op, in_versions, out_versions = self.ssa[forward_op_idx]
        g_output = list(
            input_to_grad.get(name, None) for name in forward_op.output)
        if not all(g is None for g in g_output):
            gradient_ops, g_input = GradientRegistry.GetGradientForOp(
                forward_op, g_output)
            # Checks if the gradient operators are legal
            self.CheckGradientOperators(
                forward_op_idx, gradient_ops, g_output, g_input)
            # Record the gradient map to all_input_to_grad.
            for name, grad in zip(forward_op.input, g_input):
                new_input_to_grad[name] = grad

        return new_input_to_grad, gradient_ops

    def GetBackwardPass(self, ys):
        """Gets the backward pass that computes the derivatives of given blobs.

        Inputs:
          ys: a list or a dictionary specifying what blobs we want to compute
              derivatives of. If the input is a list, we will automatically
              generate their gradients with all-one values; if the input is a
              dictionary, for any dictionary entries that are not None, we will
              take the corresponding blobs as their gradients; for all those
              that are None, we will auto-fill them with 1.
        """
        if isinstance(ys, list):
            ys = dict((y, None) for y in ys)
        elif not isinstance(ys, dict):
            raise TypeError("ys should either be a list or a dict.")

        # Set the gradient frontier with the initialized external
        # gradients.
        for y, _ in ys.items():
            self.gradient_frontier[y] = self.frontier[y]

        all_input_to_grad, all_gradient_ops = self._GetInitGradients(ys)

        # (2) Now, after having the virtual play above, we now play the ops
        # backwards, creating the gradients along the path. Note that although
        # we are playing it backwards, we cannot refer to variables that are
        # at a version older than current_versions because it is already been
        # overwritten.
        for forward_op_idx in reversed(range(len(self.ssa))):
            input_to_grad, gradient_ops = self._GenerateGradientsForForwardOp(
                forward_op_idx, all_input_to_grad)
            all_input_to_grad.update(input_to_grad)
            all_gradient_ops += gradient_ops

            # If there are multiple use blobs, do gradient accumulation.
            additional_sum_ops, grad_map = self.DoGradientAccumulation(
                forward_op_idx)
            # This line is so that if in an accumulation some of the operators
            # have not produced gradients, they still do not overwrite the
            # general all_input_to_grad map.
            all_input_to_grad.update(grad_map)
            all_gradient_ops += additional_sum_ops

        # (3) Post-processing.
        # After we have done computation for each op, we now have the gradient
        # operators ready. For the output map, we will convert everything to
        # BlobReferences for easier handling in python.
        all_input_to_grad_out = {}
        for key, val in all_input_to_grad.items():
            if val is not None:
                all_input_to_grad_out[BlobReference(key)] = (
                    BlobReference(val) if isinstance(val, basestring) else
                    GradientSlice(BlobReference(val[0]), BlobReference(val[1])))
        return all_gradient_ops, all_input_to_grad_out


class GradientRegistry(object):
    """GradientRegistry holds the mapping from operators to their gradients."""
    gradient_registry_ = {}

    @classmethod
    def RegisterGradient(cls, op_type):
        """A decorator for registering gradient mappings."""

        def Wrapper(func):
            cls.gradient_registry_[op_type] = func
            return func

        return Wrapper

    @classmethod
    def _GetGradientForOpCC(cls, op_def, g_output):
        # TODO(tulloch) - Propagate GradientWrapper up through the stack.
        def from_untyped(grad):
            if grad is None:
                w = C.GradientWrapper()
                assert w.is_empty()
                return w
            try:
                (indices, values) = grad
                w = C.GradientWrapper()
                w.indices = indices
                w.values = values
                assert w.is_sparse()
                return w
            except ValueError:
                w = C.GradientWrapper()
                w.dense = grad
                assert w.is_dense()
                return w

        g_output = [from_untyped(grad) for grad in g_output]
        grad_defs_str, g_input = C.get_gradient_defs(
            op_def.SerializeToString(), g_output)

        def to_untyped(grad_wrapper):
            if grad_wrapper.is_empty():
                return None
            if grad_wrapper.is_sparse():
                return GradientSlice(grad_wrapper.indices, grad_wrapper.values)
            assert grad_wrapper.is_dense()
            return grad_wrapper.dense

        g_input = [to_untyped(grad_wrapper) for grad_wrapper in g_input]
        grad_defs = []
        for grad_def_str in grad_defs_str:
            grad_def = caffe2_pb2.OperatorDef()
            grad_def.ParseFromString(grad_def_str)
            grad_defs.append(grad_def)
        return grad_defs, g_input

    @classmethod
    def GetGradientForOp(cls, op, g_output):
        try:
            gradient_ops, g_input = cls._GetGradientForOpCC(op, g_output)
        except Exception:
            # Not supported in C++; will try python registration next.
            try:
                gradient_ops, g_input = cls.gradient_registry_[op.type](
                    op, g_output)
            except KeyError:
                raise KeyError('No gradient registered for op: %s' % op.type)
        if gradient_ops is None:
            return [], g_input
        if type(gradient_ops) is not list:
            gradient_ops = [gradient_ops]
        return gradient_ops, g_input

    @classmethod
    def GetBackwardPass(cls, operators, ys):
        """Gets the backward pass for the list of operators.

        Args:
            operators: a list of operators constituting the forward pass.
            ys: a list or a dictionary specifying what blobs we want to compute
                derivatives of. If the input is a list, we will automatically
                generate their gradients with all-one values; if the input is a
                dictionary, for any dictionary entries that are not None, we'll
                take the corresponding blobs as their gradients; for all those
                that are None, we will auto-fill them with 1.
        Returns:
            gradient_ops: a list of gradient operators to run.
            all_input_to_grads: a map from input to their corresponding
                gradients.
        """
        ir = IR(operators)
        return ir.GetBackwardPass(ys)


def get_ssa(net, blob_versions=None):
    """
    Given a net, return a structure containing the version of each input and
    output blob used by each operator.

    Args:
        net:            either a Net or a NetDef
        blob_versions:  (optional) map with current version number for given
                        blob names. If not provided or blob not found, start
                        from version 0.
    Returns:
        Tuple (ssa, blob_versions)
        ssa:            list of tuples (versioned_inputs, versioned_outputs)
                        for each op in the net. A versioned input is a tuple
                        (blob_name, version).
        blob_versions:  updated map with latest version of each blob found in
                        the net.
    """
    proto = net.Proto() if isinstance(net, Net) else net
    assert isinstance(proto, caffe2_pb2.NetDef)
    if blob_versions is None:
        blob_versions = {}
    if isinstance(net, list):
        return [get_ssa(n, blob_versions) for n in net], blob_versions
    for i in proto.external_input:
        if i not in blob_versions:
            blob_versions[str(i)] = 0
    ssa = []
    for op in proto.op:
        if not proto.external_input:
            for i in op.input:
                if i not in blob_versions:
                    blob_versions[i] = 0
        inputs = [(str(i), blob_versions.get(str(i), 0)) for i in op.input]
        for o in op.output:
            blob_versions[str(o)] = blob_versions.get(str(o), 0) + 1
        outputs = [(str(o), blob_versions[str(o)]) for o in op.output]
        ssa.append((inputs, outputs))
    return ssa, blob_versions


def get_undefined_blobs(ssa):
    """
    Given a ssa in the format produced by get_ssa(), return a set of blobs that
    are used before they are defined, which corresponds to inputs at version 0.
    """
    undef_blobs = set()
    for inputs, outputs in ssa:
        undef_blobs |= set(name for (name, ver) in inputs if ver == 0)
    return undef_blobs


def get_output_producers(ssa):
    """
    Given a ssa in the format produced by get_ssa(), returns a map from
    versioned blob into the operator index that produces that version of
    the blob. A versioned blob is a tuple (blob_name, version).
    """
    producers = {}
    for i, (inputs, outputs) in enumerate(ssa):
        for o in outputs:
            producers[o] = i
    return producers


def get_op_ids_in_path(ssa, blob_versions, inputs, outputs):
    """
    Given a ssa and blob_versions as produced by get_ssa(), returns the list
    of op indices that are necessary in order to generate the blobs in
    `outputs`, given blobs in `inputs`.
    Consider that the `inputs` are given in their latest version.
    """
    inputs_set = set((str(i), blob_versions[str(i)]) for i in inputs)
    producers = get_output_producers(ssa)
    queue = [(str(o), blob_versions[str(o)]) for o in outputs]
    used_op_ids = set()
    while len(queue) > 0:
        o = queue.pop()
        if (o not in inputs_set) and (o in producers):
            op_id = producers[o]
            used_op_ids |= {op_id}
            inputs, _ = ssa[op_id]
            queue.extend(inputs)
    return sorted(used_op_ids)


class Net(object):
    _net_names_used = set()
    operator_registry_ = {}

    @staticmethod
    def _get_next_net_name(basename):
        name = basename
        next_idx = 1
        while name in Net._net_names_used:
            name = basename + '_' + str(next_idx)
            next_idx += 1
        Net._net_names_used |= set([name])
        return name

    def __init__(self, name_or_proto):
        """
        Create a Net.
        Args:
            name_or_proto:  If a NetDef is provided, clone it. Otherwise,
                            create an empty net with the given name.
        """
        if type(name_or_proto) is caffe2_pb2.NetDef:
            proto = name_or_proto
            # We rae initializing a network by a NetDef. In this case, we will
            # initialize our network with the given netdef.
            self._net = caffe2_pb2.NetDef()
            self._net.CopyFrom(proto)
            # Set the next name index properly.
            existing_names = set(
                sum(
                    [list(op.input) for op in self._net.op], []
                ) + sum(
                    [list(op.output) for op in self._net.op], []
                )
            )
            prefix_len = len(self._net.name + '_blob_')
            autogen_indices = []
            for s in existing_names:
                if s.startswith(self._net.name + '_blob_'):
                    try:
                        autogen_indices.append(int(s[prefix_len]))
                    except ValueError:
                        pass
            if len(autogen_indices):
                self._next_name_index = max(autogen_indices) + 1
            else:
                self._next_name_index = 0
        else:
            self._net = caffe2_pb2.NetDef()
            self._net.name = name_or_proto
            self._next_name_index = 0

        # make sure that this net name hasn't been used before
        self._net.name = Net._get_next_net_name(self._net.name)

    def __str__(self):
        return self._net.name

    def BlobIsDefined(self, blob):
        """
        Returns true if the given BlobReference is produced as output of
        an operator in this net, or if it is provided as an external input.
        """
        blob_name = str(blob)
        for input in self._net.external_input:
            if input == blob_name:
                return True
        for op in self._net.op:
            for output in op.output:
                if output == blob_name:
                    return True
        return False

    def UsesBlob(self, blob):
        """
        Returns true iff the given BlobReference is used by any operator
        or this net, or if it is one of the external inputs of the net.
        """
        blob_name = str(blob)
        for op in self._net.op:
            for input in op.input:
                if input == blob_name:
                    return True
        for input in self._net.external_input:
            if input == blob_name:
                return True
        return False

    def GetBlobRef(self, blob_name):
        """
        Given the name of a blob produced by this net, return a BlobReference
        to it. If the blob is not produced by any op in this net,
        raises KeyError.
        """
        blob_name = str(blob_name)
        if not self.BlobIsDefined(blob_name):
            raise KeyError('Net does not define blob %s' % blob_name)
        return BlobReference(blob_name, self)

    def Clone(self, name, blob_remap=None, op_id_mask=None, remap_funcs=None):
        """
        Clone this net.
        Args:
            name:        name of the cloned net
            blob_remap:  optional map with list of blob names to replace
            op_id_mask:  optional list of operator indices to include in
                         the cloned net. If not provided, all ops are included.
        """
        if remap_funcs is None:
            remap_funcs = {}
        proto = self._net
        new_proto = caffe2_pb2.NetDef()
        new_proto.CopyFrom(proto)
        new_proto.name = name
        if blob_remap is None and op_id_mask is None:
            return Net(new_proto)

        if blob_remap is None:
            blob_remap = {}
        if op_id_mask is None:
            op_id_mask = range(0, len(proto.op))

        def remap_list(proto_list):
            new_list = [blob_remap.get(b, b) for b in proto_list]
            del proto_list[:]
            proto_list.extend(new_list)

        def remap_op(op):
            new_op = caffe2_pb2.OperatorDef()
            new_op.CopyFrom(op)
            remap_list(new_op.input)
            remap_list(new_op.output)
            if new_op.type in remap_funcs:
                remap_funcs[new_op.type](new_op, (name + '/') if name else '')
            return new_op

        del new_proto.op[:]
        new_proto.op.extend(remap_op(proto.op[op_id]) for op_id in op_id_mask)
        remap_list(new_proto.external_input)
        remap_list(new_proto.external_output)
        return Net(new_proto)

    def ClonePartial(self, name, inputs, outputs, remap_funcs=None):
        """
        Clone this net, including only ops that are necessary in order to
        compute `outputs` given `inputs`. Return references to the cloned
        outputs. Internal blobs (blobs that are produced and consumed inside
        the net but not used as outputs) will be remapped to avoid name
        conflict.

        Args:
            name:    the name of the cloned net
            inputs:  map where the keys correspond to BlobReferences in the
                     original net, and the values correspond to external inputs
                     in the partially cloned net. If `inputs` is a list, don't
                     remap input names.
            outputs: outputs to be produced by the cloned net.

        Returns:
            Tuple (new_net, new_outputs)
                new_net:       a new Net object.
                new_outputs:   list of BlobReferences corresponding to the
                               outputs produced by new_net.
        """
        input_is_pair_list = isinstance(inputs, list) and all(
            isinstance(i, tuple) and len(i) == 2 for i in inputs)
        inputs = (
            inputs if isinstance(inputs, (dict, OrderedDict)) else
            OrderedDict(inputs) if input_is_pair_list else
            OrderedDict(zip(inputs, inputs)))
        for output in outputs:
            assert self.BlobIsDefined(output)
        input_names = {str(k): str(v) for k, v in inputs.items()}
        output_names = [str(o) for o in outputs]
        proto = self._net
        ssa, blob_versions = get_ssa(proto)
        used_op_ids = get_op_ids_in_path(ssa, blob_versions, inputs, outputs)
        disallowed_op_ids = get_op_ids_in_path(ssa, blob_versions, [], inputs)
        assert len(set(used_op_ids) & set(disallowed_op_ids)) == 0, (
            'Cannot partially clone net: some of the ops required would ' +
            'generate the given input.')

        sub_ssa = [op for i, op in enumerate(ssa) if i in used_op_ids]
        undef_blobs = get_undefined_blobs(sub_ssa) - set(input_names.keys())
        prefix = (name + '/') if name else ''

        def remap(blob_name):
            if blob_name in input_names:
                return input_names[blob_name]
            elif blob_name in undef_blobs:
                return blob_name
            else:
                return prefix + blob_name

        blob_mapping = {b: remap(b) for b in blob_versions.keys()}
        new_net = self.Clone(name, blob_mapping, used_op_ids, remap_funcs)
        new_in = [
            blob_mapping[i] for i in input_names.keys()] + list(undef_blobs)
        new_out = [blob_mapping[o] for o in output_names]
        del new_net.Proto().external_input[:]
        new_net.Proto().external_input.extend(new_in)
        del new_net.Proto().external_output[:]
        new_net.Proto().external_output.extend(new_out)
        return new_net, [new_net.GetBlobRef(o) for o in new_out]

    def Proto(self):
        return self._net

    def NextName(self, prefix=None, output_id=None):
        """Returns the next name to be used, if you do not want to explicitly
        name your blob."""
        if prefix:
            output_name_base = self._net.name + '/' + prefix
            output_name = output_name_base
            if output_id is not None:
                output_name += ':' + str(output_id)
            index = 2
            while self.BlobIsDefined(str(ScopedBlobReference(output_name))):
                output_name = output_name_base + '_' + str(index)
                if output_id is not None:
                    output_name += ':' + str(output_id)
                index += 1
        else:
            output_name = self._net.name + '_blob_' + str(self._next_name_index)
            self._next_name_index += 1
        return str(output_name)

    def AddGradientOperators(self, ys, skip=0):
        """Add the gradient for operators in the net.

        Inputs:
          ys: a list or a dictionary specifying what blobs we want to compute
              derivatives of. If the input is a list, we will automatically
              generate their gradients with all-one values; if the input is a
              dictionary, for any dictionary entries that are not None, we will
              take the corresponding blobs as their gradients; for all those
              that are None, we will auto-fill them with 1.
          skip: skips the first n operators. This is provided mainly because a
              lot of nets may use the first few operators for data generation
              like stuff which really do not need to have gradients.

        Outputs:
          returns a map from the blob name in the input network to a blob
          containing gradient or a GradientSlice in case of sparse gradient

        Currently, this is hard-coded for float operators if there are branches
        (i.e. a blob is used as input to multiple operators). This is because
        the gradient accumulation (Sum) is float only right now.
        """

        grad_ops, input_to_grad = GradientRegistry.GetBackwardPass(
            self._net.op[skip:], ys)
        # Check if in immediate mode: the grad_ops are actually being produced
        # by C++ and bypasses the CreateOperator() call, so in immediate mode
        # we will have to explicitly run them.
        if workspace.IsImmediate():
            for op in grad_ops:
                workspace.RunOperatorImmediate(op)
        self._net.op.extend(grad_ops)
        return input_to_grad

    def AddExternalInput(self, input):
        input_name = str(input)
        assert input_name not in self._net.external_input, (
            'Net already contains an input named %s' % input_name)
        self._net.external_input.extend([input_name])
        return (
            input if isinstance(input, BlobReference)
            else BlobReference(input_name))

    def AddExternalOutput(self, output):
        assert isinstance(output, BlobReference)
        assert self.BlobIsDefined(output)
        self.Proto().external_output.extend([str(output)])

    def DeduplicateGradientSlices(self, g):
        assert isinstance(g, GradientSlice)
        unique, remapping = self.Unique([g.indices], 2)
        sum_g = self.UnsortedSegmentSum([g.values, remapping], 1)
        return GradientSlice(indices=unique, values=sum_g)

    def RunAllOnGPU(self, gpu_id=0, use_cudnn=False):
        """A convenient function to run everything on the GPU."""
        device_option = caffe2_pb2.DeviceOption()
        device_option.device_type = caffe2_pb2.CUDA
        device_option.cuda_gpu_id = gpu_id
        self._net.device_option.CopyFrom(device_option)
        if use_cudnn:
            for op in self._net.op:
                op.engine = "CUDNN"

    def _CreateAndAddToSelf(self, op_type, inputs, outputs=None, **kwargs):
        """A helper function to create an operator and add it to self.
        """
        inputs = _RectifyInputOutput(inputs)
        for input in inputs:
            if not self.BlobIsDefined(input):
                assert input.Net() != self
                self.AddExternalInput(input)
        if outputs is None:
            # If we do not specify an output, we will assume that this op
            # produces one output in this case.
            outputs = self.NextName(prefix=op_type)
        elif type(outputs) is int:
            # In this case, we will auto-fill the given number of outputs
            # with auto-generated names.
            outputs = [
                self.NextName(prefix=op_type, output_id=i)
                for i in range(outputs)]
        outputs = _RectifyInputOutput(outputs, net=self)
        op = CreateOperator(op_type, inputs, outputs, **kwargs)
        self._net.op.extend([op])
        if len(op.output) == 0:
            return
        elif len(op.output) == 1:
            return BlobReference(str(op.output[0]), self)
        else:
            return tuple(BlobReference(str(o), self) for o in op.output)

    def __getattr__(self, op_type):
        if op_type.startswith('__'):
            raise AttributeError('Attribute {} not found.'.format(op_type))
        if not IsOperator(op_type):
            raise RuntimeError(
                'Method ' + op_type + ' is not a registered operator.'
            )
        return lambda *args, **kwargs: self._CreateAndAddToSelf(
            op_type, *args, **kwargs)

    def Python(self, f, grad_f=None):
        with extension_loader.DlopenGuard():
            import caffe2.python.op.python_ops_python as ops_python
        RefreshRegisteredOperators()
        assert(IsOperator('Python'))
        token = ops_python.register(f)
        if grad_f:
            ops_python.register_gradient(token, grad_f)
        return lambda *args, **kwargs: self._CreateAndAddToSelf(
            'Python', token=token, *args, **kwargs)


def get_net_name(netlike):
    if isinstance(netlike, Net):
        return netlike.Proto().name
    elif isinstance(netlike, caffe2_pb2.NetDef):
        return netlike.name
    else:
        return netlike


def output_to_list(op_output):
    """
    Ensures that the output of an operator is a list.
    Use when an operator has a variable number of outputs, but a list of
    outputs is desired even when number of outputs is 1.

    Args:
        op_output: Either a BlobReferenece or an iterable of BlobReferences.

    Returns:
        A list of BlobReferences.
    """
    assert type(op_output) in (list, tuple, BlobReference)
    return (
        [op_output]
        if isinstance(op_output, BlobReference) else list(op_output))


def _add_net_to_dict(net_dict, net):
    name = get_net_name(net)
    if net in net_dict:
        assert net_dict[name] is None or net == net_dict[name], (
            'Different nets with same name: ' + name)
        return False
    else:
        net_dict[name] = net if isinstance(net, Net) else None
        return True


class ExecutionStep(object):
    def __init__(self, name, nets=None, num_iter=None):
        self._step = caffe2_pb2.ExecutionStep()
        self._step.name = name
        self._net_dict = OrderedDict()
        self._is_used = False
        self._substeps = []
        if nets is not None:
            if type(nets) is Net:
                nets = [nets]
            for net in nets:
                if _add_net_to_dict(self._net_dict, net):
                    self._step.network.extend([get_net_name(net)])
        if num_iter is not None:
            self._step.num_iter = num_iter

    def Name(self):
        return self._step.name

    def __str__(self):
        return self._step.name

    def _assert_can_mutate(self):
        assert not self._is_used, (
            'Cannot mutate a step that has already been added to a plan/step.')

    def _notify_is_used(self):
        self._assert_can_mutate()
        self._is_used = True

    def Proto(self):
        return self._step

    def HasNets(self):
        return self._step.network is not None and (
            len(self._step.network) > 0)

    def HasSubsteps(self):
        return self._step.substep is not None and (
            len(self._step.substep) > 0)

    def Nets(self):
        return self._net_dict.values()

    def Substeps(self):
        return self._substeps

    def SetIter(self, num_iter):
        self._assert_can_mutate()
        self._step.num_iter = num_iter

    def SetShouldStopBlob(self, should_stop_blob):
        assert isinstance(should_stop_blob, BlobReference), (
            "expects BlobReference here, got {}".format(type(should_stop_blob)))
        self._assert_can_mutate()
        self._step.should_stop_blob = str(should_stop_blob)

    def SetReportNet(self, report_net, report_interval):
        self._assert_can_mutate()
        _add_net_to_dict(self._net_dict, report_net)
        self._step.report_net = get_net_name(report_net)
        self._step.report_interval = report_interval

    def AddSubstep(self, substep):
        self._assert_can_mutate()
        assert not self.HasNets(), 'Cannot have both network and substeps.'
        if isinstance(substep, ExecutionStep):
            substep._notify_is_used()
            if not substep.HasNets() and not substep.HasSubsteps():
                return self
            for net in substep.Nets():
                _add_net_to_dict(self._net_dict, net)
            self._substeps.append(substep)
            proto = substep.Proto()
        else:
            proto = substep
        self._step.substep.add().CopyFrom(proto)
        return self

    def SetConcurrentSubsteps(self, concurrent_substeps):
        self._assert_can_mutate()
        assert not self.HasNets(), 'Cannot have both network and substeps.'
        self._step.concurrent_substeps = concurrent_substeps

    def AddNet(self, net):
        self._assert_can_mutate()
        assert not self.HasSubsteps(), 'Cannot have both network and substeps.'
        assert isinstance(net, Net)
        _add_net_to_dict(self._net_dict, net)
        self._step.network.extend([get_net_name(net)])
        return self


class Plan(object):
    def __init__(self, name_or_step):
        self._plan = caffe2_pb2.PlanDef()
        self._net_dict = OrderedDict()
        if isinstance(name_or_step, ExecutionStep):
            self._plan.name = name_or_step.Name()
            self.AddStep(name_or_step)
        elif isinstance(name_or_step, basestring):
            self._plan.name = name_or_step
        else:
            raise ValueError('name_or_step must be a string or ExecutionStep')

    def __str__(self):
        return self._plan.name

    def Proto(self):
        return self._plan

    def AddNets(self, nets):
        for net in nets:
            if _add_net_to_dict(self._net_dict, net):
                assert isinstance(net, Net)
                self._plan.network.add().CopyFrom(net.Proto())

    def Nets(self):
        return self._net_dict.values()

    def AddStep(self, step):
        assert isinstance(step, ExecutionStep)
        step._notify_is_used()
        if not step.HasNets() and not step.HasSubsteps():
            return
        self._plan.execution_step.add().CopyFrom(step.Proto())
        self.AddNets(step.Nets())


def execution_step(default_name,
                   steps_or_nets,
                   num_iter=None,
                   report_net=None,
                   report_interval=None,
                   concurrent_substeps=None,
                   should_stop_blob=None):
    """
    Helper for creating an ExecutionStep.
    - steps_or_nets can be:
      - None
      - Net
      - ExecutionStep
      - list<Net>
      - list<ExecutionStep>
    - should_stop_blob is either None or a scalar boolean blob.
      - This blob is checked AFTER every substeps/subnets.
      - If specified and true, then this step will return immediately.
      - Be sure to handle race conditions if setting from concurrent threads.
    - if no should_stop_blob or num_iter is provided, defaults to num_iter=1
    """
    assert should_stop_blob is None or num_iter is None, (
        'Cannot set both should_stop_blob and num_iter.')
    if should_stop_blob is None and num_iter is None:
        num_iter = 1

    def set_step_attr(step):
        if should_stop_blob is not None:
            step.SetShouldStopBlob(should_stop_blob)
        else:
            step.SetIter(num_iter)
        if concurrent_substeps is not None:
            step.SetConcurrentSubsteps(concurrent_substeps)
        if report_net is not None:
            assert report_interval is not None
            step.SetReportNet(report_net, report_interval)
        return step

    if not steps_or_nets:
        return ExecutionStep(default_name)
    if isinstance(steps_or_nets, ExecutionStep):
        step = set_step_attr(ExecutionStep(default_name))
        step.AddSubstep(steps_or_nets)
        return step
    elif isinstance(steps_or_nets, Net):
        step = set_step_attr(ExecutionStep(default_name))
        step.AddNet(steps_or_nets)
        return step
    elif isinstance(steps_or_nets, list):
        step = set_step_attr(ExecutionStep(default_name))
        for step_or_net in steps_or_nets:
            if isinstance(step_or_net, Net):
                step.AddNet(step_or_net)
            elif isinstance(step_or_net, ExecutionStep):
                step.AddSubstep(step_or_net)
            else:
                raise ValueError('unsupported type {}'.format(step_or_net))
        return step
    else:
        raise ValueError(
            'steps_or_nets must be a step, a net, or a list of nets or steps.')

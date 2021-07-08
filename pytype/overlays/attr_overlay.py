"""Support for the 'attrs' library."""

import logging

from pytype import abstract
from pytype import abstract_utils
from pytype import function
from pytype import mixin
from pytype import overlay
from pytype import overlay_utils
from pytype.overlays import classgen

log = logging.getLogger(__name__)

# type aliases for convenience
Param = overlay_utils.Param
Attribute = classgen.Attribute


class AttrOverlay(overlay.Overlay):
  """A custom overlay for the 'attr' module."""

  def __init__(self, vm):
    member_map = {
        "attrs": Attrs.make,
        "attrib": Attrib.make,
        "s": Attrs.make,
        "ib": Attrib.make,
        "Factory": Factory.make,
    }
    ast = vm.loader.import_name("attr")
    super().__init__(vm, "attr", member_map, ast)


class Attrs(classgen.Decorator):
  """Implements the @attr.s decorator."""

  @classmethod
  def make(cls, vm):
    return super().make("s", vm, "attr")

  def init_name(self, attr):
    # attrs removes leading underscores from attrib names when generating kwargs
    # for __init__.
    return attr.name.lstrip("_")

  def decorate(self, node, cls):
    """Processes the attrib members of a class."""
    # Collect classvars to convert them to attrs.
    if self.args[cls]["auto_attribs"]:
      ordering = classgen.Ordering.FIRST_ANNOTATE
    else:
      ordering = classgen.Ordering.LAST_ASSIGN
    ordered_locals = classgen.get_class_locals(
        cls.name, allow_methods=False, ordering=ordering, vm=self.vm)
    own_attrs = []
    for name, local in ordered_locals.items():
      typ, orig = local.get_type(node, name), local.orig
      if is_attrib(orig):
        attrib = orig.data[0]
        if typ and attrib.has_type:
          # We cannot have both a type annotation and a type argument.
          self.vm.errorlog.invalid_annotation(self.vm.frames, typ)
          attr = Attribute(
              name=name,
              typ=self.vm.convert.unsolvable,
              init=attrib.init,
              kw_only=attrib.kw_only,
              default=attrib.default)
        elif not typ:
          # Replace the attrib in the class dict with its type.
          attr = Attribute(
              name=name,
              typ=attrib.typ,
              init=attrib.init,
              kw_only=attrib.kw_only,
              default=attrib.default)
          classgen.add_member(node, cls, name, attr.typ)
          if attrib.has_type and isinstance(cls, abstract.InterpreterClass):
            # Add the attrib to the class's __annotations__ dict.
            annotations_dict = abstract_utils.get_annotations_dict(cls.members)
            if annotations_dict is None:
              annotations_dict = abstract.AnnotationsDict({}, self.vm)
              cls.members["__annotations__"] = annotations_dict.to_variable(
                  self.vm.root_node)
            annotations_dict.annotated_locals[name] = abstract_utils.Local(
                node, None, attrib.typ, orig, self.vm)
        else:
          # cls.members[name] has already been set via a typecomment
          attr = Attribute(
              name=name,
              typ=typ,
              init=attrib.init,
              kw_only=attrib.kw_only,
              default=attrib.default)
        self.vm.check_annotation_type_mismatch(
            node, attr.name, attr.typ, attr.default, local.stack,
            allow_none=True)
        own_attrs.append(attr)
      elif self.args[cls]["auto_attribs"]:
        if not match_classvar(typ):
          self.vm.check_annotation_type_mismatch(
              node, name, typ, orig, local.stack, allow_none=True)
          attr = Attribute(
              name=name, typ=typ, init=True, kw_only=False, default=orig)
          if not orig:
            classgen.add_member(node, cls, name, typ)
          own_attrs.append(attr)

    cls.record_attr_ordering(own_attrs)
    attrs = cls.compute_attr_metadata(own_attrs, "attr.s")

    # Add an __init__ method
    if self.args[cls]["init"]:
      init_method = self.make_init(node, cls, attrs)
      cls.members["__init__"] = init_method

    if isinstance(cls, abstract.InterpreterClass):
      cls.decorators.append("attr.s")
      # Fix up type parameters in methods added by the decorator.
      cls.update_method_type_params()


class AttribInstance(abstract.SimpleValue, mixin.HasSlots):
  """Return value of an attr.ib() call."""

  def __init__(self, vm, typ, has_type, init, kw_only, default):
    super().__init__("attrib", vm)
    mixin.HasSlots.init_mixin(self)
    self.typ = typ
    self.has_type = has_type
    self.init = init
    self.kw_only = kw_only
    self.default = default
    # TODO(rechen): attr.ib() returns an instance of attr._make._CountingAttr.
    self.cls = vm.convert.unsolvable
    self.set_slot("default", self.default_slot)
    self.set_slot("validator", self.validator_slot)

  def default_slot(self, node, default):
    # If the default is a method, call it and use its return type.
    fn = default.data[0]
    # TODO(mdemello): it is not clear what to use for self in fn_args; using
    # fn.cls.instantiate(node) is fraught because we are in the process of
    # constructing the class. If fn does not use `self` setting self=Any will
    # make no difference; if it does use `self` we might as well fall back to a
    # return type of `Any` rather than raising attribute errors in cases like
    # class A:
    #   x = attr.ib(default=42)
    #   y = attr.ib()
    #   @y.default
    #   def _y(self):
    #     return self.x
    #
    # The correct thing to do would probably be to defer inference if we see a
    # default method, then infer all the method-based defaults after the class
    # is fully constructed. The workaround is simply to use type annotations,
    # which users should ideally be doing anyway.
    self_var = self.vm.new_unsolvable(node)
    fn_args = function.Args(posargs=(self_var,))
    node, default_var = fn.call(node, default.bindings[0], fn_args)
    self.default = default_var
    # If we don't have a type, set the type from the default type
    if not self.has_type:
      self.typ = get_type_from_default(default_var, self.vm)
    # Return the original decorated method so we don't lose it.
    return node, default

  def validator_slot(self, node, validator):
    return node, validator


class Attrib(classgen.FieldConstructor):
  """Implements attr.ib."""

  @classmethod
  def make(cls, vm):
    return super().make("ib", vm, "attr")

  def call(self, node, unused_func, args):
    """Returns a type corresponding to an attr."""
    args = args.simplify(node, self.vm)
    if isinstance(args.namedargs, mixin.PythonConstant):
      # Remove the 'type' argument from args so that it doesn't trigger
      # match_args' "cannot pass a TypeVar to a function" check.
      # TODO(rechen): consider getting rid of this check altogether; it makes
      # using types at runtime difficult and sometimes triggers incorrectly.
      try:
        type_var = args.namedargs.pyval.pop("type")
      except KeyError:
        type_var = None
    else:
      type_var = None
    self.match_args(node, args)
    node, default_var = self._get_default_var(node, args)
    init = self.get_kwarg(args, "init", True)
    kw_only = self.get_kwarg(args, "kw_only", False)
    has_type = type_var is not None
    if type_var:
      allowed_type_params = (
          self.vm.frame.type_params |
          self.vm.annotations_util.get_callable_type_parameter_names(type_var))
      typ = self.vm.annotations_util.extract_annotation(
          node, type_var, "attr.ib", self.vm.simple_stack(),
          allowed_type_params=allowed_type_params, use_not_supported_yet=False)
    elif default_var:
      typ = get_type_from_default(default_var, self.vm)
    else:
      typ = self.vm.convert.unsolvable
    typ = AttribInstance(
        self.vm, typ, has_type, init, kw_only, default_var).to_variable(node)
    return node, typ

  def _get_default_var(self, node, args):
    if "default" in args.namedargs and "factory" in args.namedargs:
      # attr.ib(factory=x) is syntactic sugar for attr.ib(default=Factory(x)).
      raise function.DuplicateKeyword(self.signatures[0].signature, args,
                                      self.vm, "default")
    elif "default" in args.namedargs:
      default_var = args.namedargs["default"]
    elif "factory" in args.namedargs:
      mod = self.vm.import_module("attr", "attr", 0)
      node, attr = self.vm.attribute_handler.get_attribute(node, mod, "Factory")
      # We know there is only one value because Factory is in the overlay.
      factory, = attr.data
      factory_args = function.Args(posargs=(args.namedargs["factory"],))
      node, default_var = factory.call(node, attr.bindings[0], factory_args)
    else:
      default_var = None
    return node, default_var


def is_attrib(var):
  return var and isinstance(var.data[0], AttribInstance)


def match_classvar(typ):
  """Unpack the type parameter from ClassVar[T]."""
  return abstract_utils.match_type_container(typ, "typing.ClassVar")


def get_type_from_default(default_var, vm):
  """Get the type of an attribute from its default value."""
  if default_var.data == [vm.convert.none]:
    # A default of None doesn't give us any information about the actual type.
    return vm.convert.unsolvable
  typ = vm.convert.merge_classes(default_var.data)
  if typ == vm.convert.empty:
    return vm.convert.unsolvable
  elif isinstance(typ, abstract.TupleClass) and not typ.tuple_length:
    # The type of an attribute whose default is an empty tuple should be
    # Tuple[Any, ...], not Tuple[()].
    return vm.convert.tuple_type
  return typ


class Factory(abstract.PyTDFunction):
  """Implementation of attr.Factory."""

  @classmethod
  def make(cls, vm):
    return super().make("Factory", vm, "attr")

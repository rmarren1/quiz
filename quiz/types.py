"""main module for constructing graphQL queries"""
import abc
import enum
import json
import typing as t
from collections import ChainMap, defaultdict
from dataclasses import dataclass, replace
from functools import partial, singledispatch
from itertools import chain
from operator import attrgetter, methodcaller
from textwrap import indent

import snug

from . import schema
from .utils import Error, FrozenDict

ClassDict = t.Dict[str, type]
NoneType = type(None)
INDENT = "  "

gql = methodcaller("__gql__")

FieldName = str
"""a valid GraphQL fieldname"""


@singledispatch
def argument_as_gql(obj: object) -> str:
    raise TypeError("cannot serialize to GraphQL: {}".format(type(obj)))


# TODO: IMPORTANT! string escape
argument_as_gql.register(str, '"{}"'.format)

# TODO: limit to 32 bit integers!
argument_as_gql.register(int, str)
argument_as_gql.register(NoneType, 'null'.format)
argument_as_gql.register(bool, {True: 'true', False: 'false'}.__getitem__)

# TODO: float, with exponent form
# TODO: long (when py2 support is needed)


@argument_as_gql.register(enum.Enum)
def _enum_to_gql(obj):
    return obj.value


# TODO: add fragmentspread
Selection = t.Union['Field', 'InlineFragment']


# TODO: ** operator for specifying fragments
@dataclass(repr=False, frozen=True, init=False)
class SelectionSet(t.Iterable[Selection], t.Sized):
    """A "magic" selection set builder"""
    # the attribute needs to have a dunder name to prevent
    # comflicts with GraphQL field names
    __selections__: t.Tuple[Selection]
    # according to the GQL spec: this is ordered

    # why can't this subclass tuple?
    # Then we would have unwanted methods like index()

    def __init__(self, *selections):
        self.__dict__['__selections__'] = selections

    # TODO: optimize
    @classmethod
    def _make(cls, selections):
        return cls(*selections)

    def __getattr__(self, name):
        return SelectionSet._make(self.__selections__ + (Field(name), ))

    # TODO: support raw graphql strings
    def __getitem__(self, selection_set):
        # TODO: check duplicate fieldnames
        try:
            *rest, target = self.__selections__
        except ValueError:
            raise Error('cannot select fields from empty field list')

        assert isinstance(selection_set, SelectionSet)
        assert len(selection_set.__selections__) >= 1

        return SelectionSet._make(
            tuple(rest)
            + (replace(target, selection_set=selection_set), ))

    def __repr__(self):
        return "<SelectionSet> {}".format(gql(self))

    # TODO: prevent `self` from conflicting with kwargs
    def __call__(self, **kwargs):
        try:
            *rest, target = self.__selections__
        except ValueError:
            raise Error('cannot call empty field list')
        return SelectionSet._make(
            tuple(rest) + (replace(target, kwargs=FrozenDict(kwargs)), ))

    def __iter__(self):
        return iter(self.__selections__)

    def __len__(self):
        return len(self.__selections__)

    def __gql__(self) -> str:
        return '{{\n{}\n}}'.format(
            '\n'.join(
                indent(gql(f), INDENT) for f in self.__selections__
            )
        ) if self.__selections__ else ''

    __hash__ = property(attrgetter('__selections__.__hash__'))

    def __eq__(self, other):
        if isinstance(other, self.__class__):
            return other.__selections__ == self.__selections__
        return NotImplemented

    def __ne__(self, other):
        equality = self.__eq__(other)
        return NotImplemented if equality is NotImplemented else not equality


@dataclass(frozen=True)
class Raw:
    gql: str

    def __gql__(self):
        return self.gql


@dataclass(frozen=True)
class Field:
    name: FieldName
    kwargs: FrozenDict = FrozenDict.EMPTY
    selection_set: SelectionSet = SelectionSet()
    # TODO:
    # - alias
    # - directives

    def __gql__(self):
        arguments = '({})'.format(
            ', '.join(
                "{}: {}".format(k, argument_as_gql(v))
                for k, v in self.kwargs.items()
            )
        ) if self.kwargs else ''
        selection_set = (
            ' ' + gql(self.selection_set)
            if self.selection_set else '')
        return self.name + arguments + selection_set


selector = SelectionSet()


class ID(str):
    """represents a unique identifier, often used to refetch an object
    or as the key for a cache. The ID type is serialized in the same way
    as a String; however, defining it as an ID signifies that it is not
    intended to be human‐readable"""


BUILTIN_SCALARS = {
    "Boolean": bool,
    "String":  str,
    "ID":      ID,
    "Float":   float,
    "Int":     int,
}


@dataclass(frozen=True)
class NoSuchField(Error):
    on: type
    name: str


@dataclass(frozen=True)
class NoSuchArgument(Error):
    on: type
    field: 'FieldSchema'
    name: str


@dataclass(frozen=True)
class InvalidArgumentType(Error):
    on: type
    field: 'FieldSchema'
    name: str
    value: object


@dataclass(frozen=True)
class MissingArgument(Error):
    on: type
    field: 'FieldSchema'
    name: str


@dataclass(frozen=True)
class InvalidSelection(Error):
    on: type
    field: 'FieldSchema'


@dataclass(frozen=True)
class InlineFragment:
    on: type
    selection_set: SelectionSet
    # TODO: add directives

    def __gql__(self):
        return '... on {} {}'.format(
            self.on.__name__,
            gql(self.selection_set)
        )


class OperationType(enum.Enum):
    QUERY = 'query'
    MUTATION = 'mutation'
    SUBSCRIPTION = 'subscription'


@dataclass(frozen=True)
class Operation:
    type: OperationType
    selection_set: SelectionSet = SelectionSet()
    # TODO:
    # - name (optional)
    # - variable_defs (optional)
    # - directives (optional)

    def __gql__(self):
        return '{} {}'.format(self.type.value,
                              gql(self.selection_set))


def _is_optional(typ: type) -> bool:
    """check whether a type is a typing.Optional"""
    try:
        return typ.__origin__ is t.Union and NoneType in typ.__args__
    except AttributeError:
        return False


def _unwrap_type(type_: type) -> type:
    if _is_optional(type_):
        return _unwrap_type(
            t.Union[tuple(c for c in type_.__args__
                          if c is not NoneType)])
    elif getattr(type_, '__origin__', None) is list:
        return _unwrap_type(type_.__args__[0])
    return type_


def _unwrap_union(type_: type) -> t.Union[type, t.Tuple[type, ...]]:
    try:
        if type_.__origin__ is t.Union:
            return type_.__args__
    except AttributeError:
        pass
    return type_


def _check_args(cls, field, kwargs) -> t.NoReturn:
    invalid_args = kwargs.keys() - field.args.keys()
    if invalid_args:
        raise NoSuchArgument(cls, field, invalid_args.pop())

    for param in field.args.values():
        try:
            value = kwargs[param.name]
        except KeyError:
            if not _is_optional(param.type):
                raise MissingArgument(cls, field, param.name)
        else:
            if not isinstance(value, _unwrap_union(param.type)):
                raise InvalidArgumentType(
                    cls, field, param.name, value
                )


def _check_field(parent, field) -> t.NoReturn:
    assert isinstance(field, Field)
    try:
        schema = getattr(parent, field.name)
    except AttributeError:
        raise NoSuchField(parent, field.name)

    _check_args(parent, schema, field.kwargs)

    for f in field.selection_set:
        _check_field(_unwrap_type(schema.type), f)


# inherit from ABCMeta to allow mixing with other ABCs
class ObjectMeta(abc.ABCMeta):

    def __getitem__(self, selection_set: SelectionSet) -> InlineFragment:
        for field in selection_set:
            _check_field(self, field)
        return InlineFragment(self, selection_set)

    # TODO: prevent direct instantiation


class Object(metaclass=ObjectMeta):
    """a graphQL object"""


# - InputObject: calling instantiates an instance,
#   results must be instances of the class
class InputObject:
    pass


# separate class to distinguish graphql enums from normal Enums
# TODO: include deprecation attributes in instances?
# TODO: a __repr__ which includes the description, deprecation, etc?
class Enum(enum.Enum):
    pass


# TODO: this should be a metaclass
class Interface:
    pass


class InputValue(t.NamedTuple):
    name: str
    desc: str
    type: type


# TODO: nice repr for help() display
class FieldSchema(t.NamedTuple):
    name: str
    desc: str
    type: type
    args: FrozenDict  # TODO: use type parameters
    is_deprecated: bool
    deprecation_reason: t.Optional[str]


def _namedict(classes):
    return {c.__name__: c for c in classes}


def object_as_type(typ: schema.Object,
                   interfaces: t.Mapping[str, type(Interface)]) -> type:
    return type(
        typ.name,
        tuple(interfaces[i.name] for i in typ.interfaces) + (Object, ),
        {"__doc__": typ.desc, "__schema__": typ},
    )


# NOTE: fields are not added yet. These must be added later with _add_fields
# why is this? Circular references may exist, which can only be added
# after all classes have been defined
def interface_as_type(typ: schema.Interface):
    return type(typ.name, (Interface, ),
                {"__doc__": typ.desc, '__schema__': typ})


def enum_as_type(typ: schema.Enum) -> t.Type[enum.Enum]:
    # TODO: convert camelcase to snake-case?
    cls = Enum(typ.name, {v.name: v.name for v in typ.values})
    cls.__doc__ = typ.desc
    for member, conf in zip(cls.__members__.values(), typ.values):
        member.__doc__ = conf.desc
    return cls


# TODO: better error handling:
# - empty list of types
# - types not found
# python flattens unions, this is OK because GQL does not allow nested unions
def union_as_type(typ: schema.Union, objs: ClassDict):
    union = t.Union[tuple(objs[o.name] for o in typ.types)]
    union.__name__ = typ.name
    union.__doc__ = typ.desc
    return union


def inputobject_as_type(typ: schema.InputObject):
    return type(typ.name, (), {"__doc__": typ.desc})


def _add_fields(obj, classes) -> None:
    for f in obj.__schema__.fields:
        setattr(
            obj,
            f.name,
            FieldSchema(
                name=f.name,
                desc=f.name,
                args=FrozenDict({
                    i.name: InputValue(
                        name=i.name,
                        desc=i.desc,
                        type=resolve_typeref(i.type, classes),
                    )
                    for i in f.args
                }),
                is_deprecated=f.is_deprecated,
                deprecation_reason=f.deprecation_reason,
                type=resolve_typeref(f.type, classes),
            ),
        )
    del obj.__schema__
    return obj


def resolve_typeref(ref: schema.TypeRef, classes: ClassDict) -> type:
    if ref.kind is schema.Kind.NON_NULL:
        return _resolve_typeref_required(ref.of_type, classes)
    else:
        return t.Optional[_resolve_typeref_required(ref, classes)]


# TODO: exception handling
def _resolve_typeref_required(ref, classes) -> type:
    assert ref.kind is not schema.Kind.NON_NULL
    if ref.kind is schema.Kind.LIST:
        return t.List[resolve_typeref(ref.of_type, classes)]
    return classes[ref.name]


# TODO: set __module__
def build_schema(types: t.Iterable[schema.TypeSchema],
                 scalars: ClassDict) -> ClassDict:

    by_kind = defaultdict(list)
    for tp in types:
        by_kind[tp.__class__].append(tp)

    scalars_ = ChainMap(scalars, BUILTIN_SCALARS)
    undefined_scalars = {
        tp.name for tp in by_kind[schema.Scalar]} - scalars_.keys()
    if undefined_scalars:
        # TODO: special exception class
        raise Exception('Undefined scalars: {}'.format(list(
            undefined_scalars)))

    interfaces = _namedict(map(interface_as_type, by_kind[schema.Interface]))
    enums = _namedict(map(enum_as_type, by_kind[schema.Enum]))
    objs = _namedict(map(
        partial(object_as_type, interfaces=interfaces),
        by_kind[schema.Object],
    ))
    unions = _namedict(map(
        partial(union_as_type, objs=objs),
        by_kind[schema.Union]
    ))
    input_objects = _namedict(map(
        inputobject_as_type,
        by_kind[schema.InputObject]
    ))

    classes = ChainMap(
        scalars_, interfaces, enums, objs, unions, input_objects
    )

    # we can only add fields after all classes have been created.
    for obj in chain(objs.values(), interfaces.values()):
        _add_fields(obj, classes)

    return classes


def query(selection_set, cls: type) -> Operation:
    """Create a query operation

    selection_set
        The selection set
    cls
        The query type
    """
    for field in selection_set:
        _check_field(cls, field)
    return Operation(OperationType.QUERY, selection_set)


def _as_http(operation: Operation, url: str) -> snug.Query['JSON']:
    response = yield snug.Request('POST', url, json.dumps({
        'query': gql(operation),
    }).encode('ascii'), headers={'Content-Type': 'application/json'})
    content = json.loads(response.content)
    if 'errors' in content:
        # TODO: special exception class
        raise Exception(content['errors'])
    return content['data']


def execute(operation: Operation, url: str, **kwargs) -> 'JSON':
    return snug.execute(_as_http(operation, url), **kwargs)


def executor(url: str, **kwargs) -> t.Callable[[Operation], 'JSON']:
    return partial(execute, url=url, **kwargs)


introspection_query = Operation(
    OperationType.QUERY,
    Raw(schema.INTROSPECTION_QUERY)
)

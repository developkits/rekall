# Rekall Memory Forensics
# Copyright (C) 2012 Michael Cohen
# Copyright 2014 Google Inc. All Rights Reserved.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or (at
# your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307 USA
#

"""This module implements a JSON render.

A renderer is used by plugins to produce formatted output.

This code is tested in plugins/tools/render_test.py
"""
import json
import logging
import sys

from rekall import addrspace
from rekall import constants
from rekall import utils
from rekall.ui import renderer as renderer_module


class DecodingError(KeyError):
    """Raised if there is a decoding error."""

class EncodingError(KeyError):
    """Raised if we can not encode the object properly."""


class JsonObjectRenderer(renderer_module.ObjectRenderer):
    """An ObjectRenderer for Json encoding.

    For the JsonRenderer we convert objects into json safe python primitives
    (These must be json serializable).
    """
    renderers = ["JsonRenderer"]

    def _encode_value(self, item, **options):
        object_renderer_cls = self.ForTarget(item, self.renderer)

        return object_renderer_cls(
            session=self.session,
            renderer=self.renderer).EncodeToJsonSafe(item, **options)

    def render_row(self, item, **options):
        """The Json object renderer returns a json safe object for encoding."""
        self.EncodeToJsonSafe(item, **options)

    def EncodeToJsonSafe(self, item, **options):
        """Convert the item into a JSON safe item.

        JSON is only capable of encoding some simple types (dict, list, int,
        float, unicode strings etc). This method is called to convert the item
        to one of these representations. Note that this method will be called on
        the ObjectRenderer instance with a renders_type attribute which appears
        on the item's MRO.

        Args:
          item: A python object derived from the class mentioned in the
            renders_type attribite.

        Returns:
          A JSON serializable object (e.g. dict, list, unicode string etc).
        """
        if item == None:
            return None

        # If it is a plain dict we just use it as is.
        elif item.__class__ is dict:
            # Assume keys are strings.
            result = {}
            for k, v in item.items():
                result[k] = self._encode_value(v, **options)

            return result

        # Mark encoded lists so we know they are encoded.
        elif isinstance(item, (tuple, list)):
            return list(self._encode_value(x, **options) for x in item)

        # Encode json safe items literally.
        if isinstance(item, (unicode, int, long, float)):
            return item

        # JSON can not encode raw strings so we must base64 escape them. We
        # encode a bare string as a list starting with "+".
        elif isinstance(item, str):
            try:
                # If the string happens to be unicode safe we dont need to
                # encode it, but we still must mark it with a "*" to ensure the
                # decoder replaces it with a plain string.
                return ["*", unicode(item)]
            except UnicodeError:
                b64 = unicode(item.encode("base64")).rstrip("\n")
                return ["+", self._encode_value(b64)]

        # Special encoding for sets.
        elif item.__class__ is set:
            return dict(
                type="set",
                data=self.EncodeToJsonSafe(list(item)))

        # This will encode unknown objects as None. We do not raise an error
        # here in order to succeed in the encoding of arbitrary data. For
        # example, the session object may contain all kinds of unserializable
        # objects but we want to ensure we can serialize the session (albeit
        # with the loss of some of the attributes).
        logging.error("Unable to encode objects of type %s", type(item))
        return None

    def DecodeFromJsonSafe(self, value, options):
        """Decode the item from its Json safe representation.

        This should essentially be the reverse of EncodeToJsonSafe(). Each
        ObjectRenderer class should implement this method to invert
        EncodeToJsonSafe().

        Args:
          value: The json safe object to decode.
          options: A dict which will receive any options encoded by the encoder.

        Returns:
          A python object.
        """
        if value == None:
            return None

        if value.__class__ is dict:
            result = dict()
            for k, v in value.items():
                result[k] = self.renderer.decoder.Decode(v, options)

            return result

        if value.__class__ in (list, tuple):
            if not value:
                return []

            # Decode marked lists.
            if value[0] == "+":
                return self.lexicon[value[1]].decode("base64")

            elif value[0] == "*":
                return str(value[1])

            else:
                return list(self.DecodeFromJsonSafe(x, options) for x in value)

        # Decode json safe items literally.
        if isinstance(value, (unicode, int, long, float)):
            return value

        return value


class StateBasedObjectRenderer(JsonObjectRenderer):
    """An object renderer which serializes an object to a dict."""
    renders_type = ""  # Baseclass - does not act by itself.

    @classmethod
    def cache_key(cls, item):
        return item.get("id")

    def GetState(self, item, **_):
        _ = item
        return {}

    def DecodeFromJsonSafe(self, value, options):
        value.pop("id", None)
        return super(StateBasedObjectRenderer, self).DecodeFromJsonSafe(
            value, options)

    def EncodeToJsonSafe(self, item, **options):
        state = self.GetState(item, **options)
        if state.__class__ is not dict:
            raise EncodingError(
                "%s.GetState method must return a plain dict." %
                self.__class__.__name__)

        # Store both the class of the ObjectRenderer and the class name of the
        # item.
        state["type"] = unicode(self.__class__.__name__)

        try:
            object_id = item._object_id # pylint: disable=protected-access
            state["id"] = object_id
        except AttributeError:
            pass

        return self._encode_value(state)


class BaseObjectRenderer(StateBasedObjectRenderer):
    renders_type = "BaseObject"

    def DecodeFromJsonSafe(self, value, options):
        value = super(BaseObjectRenderer, self).DecodeFromJsonSafe(
            value, options)

        profile = value.pop("profile")
        value.pop("type")

        return profile.Object(**value)

    def GetState(self, item, **_):
        return dict(offset=item.obj_offset,
                    type_name=unicode(item.obj_type),
                    name=unicode(item.obj_name),
                    vm=item.obj_vm,
                    profile=item.obj_profile
                    )


class BaseAddressSpaceObjectRenderer(StateBasedObjectRenderer):
    renders_type = "BaseAddressSpace"

    def DecodeFromJsonSafe(self, value, options):
        value = super(BaseAddressSpaceObjectRenderer,
                      self).DecodeFromJsonSafe(value, options)

        cls_name = value.pop("cls")
        cls = addrspace.BaseAddressSpace.classes[cls_name]
        return cls(session=self.session, **value)

    def GetState(self, item, **_):
        result = dict(cls=unicode(item.__class__.__name__))
        if item.base is not item:
            result["base"] = item.base

        return result


class JSTreeNodeRenderer(StateBasedObjectRenderer):
    renders_type = "TreeNode"

    def DecodeFromJsonSafe(self, state, options):
        state = super(JSTreeNodeRenderer, self).DecodeFromJsonSafe(
            state, options)

        state.pop("type")
        result = state.pop("child")
        options.update(state)

        return result

    def GetState(self, item, **options):
        result = options
        result["child"] = item

        return result


class JsonEncoder(object):
    def __init__(self, session=None, compression=False, renderer=None):
        self.compression = compression
        self.renderer = renderer
        self.session = session

        # Maps lexicon id to a json safe object.
        self.lexicon = {}

        # Maps json safe objects into a lexicon id.
        self.reverse_lexicon = {}

        # A counter used to generate a unique id in the lexicon.
        self.lexicon_counter = 0

        self.cache = {}

    def GetLexicon(self):
        return self.lexicon

    def flush(self):
        self.lexicon.clear()
        self.reverse_lexicon.clear()
        self.lexicon_counter = 0

    def _get_encoded_id(self, value):
        """Gets the lexicon id of the value.

        If the value does not exist in the lexicon, make a new ID and store the
        value in the lexicon.

        Args:
          value: A Json safe python object. NOTE: This must also be hasheable.
        """
        encoded_id = self.reverse_lexicon.get(value)
        if encoded_id is None:
            self.lexicon_counter += 1
            encoded_id = str(self.lexicon_counter)
            self.reverse_lexicon[value] = encoded_id
            self.lexicon[encoded_id] = value

        return encoded_id

    def Encode(self, item, type=None, **options):
        """Convert item to a json safe object."""
        if type is None:
            object_renderer_cls = JsonObjectRenderer.ForTarget(
                item, self.renderer)
        else:
            object_renderer_cls = JsonObjectRenderer.ByName(
                type, self.renderer)

        if not object_renderer_cls:
            raise EncodingError("Unable to find ObjectRenderer class.")

        # Get a Json Safe item.
        object_renderer = object_renderer_cls(
            session=self.session, renderer=self.renderer)

        json_safe_item = object_renderer.EncodeToJsonSafe(item, **options)

        # If compression is enabled we compress as well.
        if self.compression:
            return self.Compress(json_safe_item)

        return json_safe_item

    def Compress(self, item):
        """Compresses the item based on the lexicon.

        Args:
          item: A json safe object (e.g. as obtained by the Encode() method.

        Returns:
          A compressed object. Callers need to also obtain the lexicon using
          GetLexicon() in order to decode the data.
        """
        if isinstance(item, dict):
            # Compressed dicts are marked as such.
            result = {"_": 1}
            for k, v in item.items():
                result[self.Compress(k)] = self.Compress(v)

            return result

        return self._get_encoded_id(item)


class _Empty(object):
    """An empty class to access the real instance later."""
    def __init__(self, session):
        self.session = session


class JsonDecoder(object):
    """A Decoder for JSON encoded data."""

    def __init__(self, session, renderer):
        self.session = session
        self.renderer = renderer
        self.lexicon = {}

    def SetLexicon(self, lexicon):
        self.lexicon = lexicon

    def _decompress_value(self, value):
        try:
            return self.lexicon[str(value)]
        except KeyError:
            raise DecodingError("Lexicon corruption: Tag %s" % value)

    def Decompress(self, item, options):
        if "_" in item:
            state = {}
            for k, v in item.items():
                if k == "_":
                    continue

                decoded_key = self._decompress_value(k)
                decoded_value = self._decompress_value(v)
                if decoded_value.__class__ is dict:
                    decoded_value = self.Decode(decoded_value, options)

                state[decoded_key] = decoded_value

            return state

        return item

    def ObjectRendererFromMRO(self, mro):
        """Get the best object renderer class from the MRO."""
        # MRO is the list of object inheritance for each type. For example:
        # FileAddressSpace,FDAddressSpace,BaseAddressSpace.
        for class_name in mro.split(","):
            object_renderer_cls = JsonObjectRenderer.ByName(
                class_name, self.renderer)

            if object_renderer_cls:
                return object_renderer_cls

    def Decode(self, item, options=None):
        if options is None:
            options = {}

        # Find the correct ObjectRenderer that we can use to decode this item.
        if isinstance(item, dict) and "type" in item:
            # object_renderer_cls = self.ObjectRendererFromMRO(item["type"])
            object_renderer_cls = JsonObjectRenderer.classes[item["type"]]
        else:
            object_renderer_cls = JsonObjectRenderer.ForTarget(
                item, "JsonRenderer")

        object_renderer = object_renderer_cls(
            session=self.session,
            renderer=self.renderer)

        key = object_renderer_cls.cache_key(item)
        if key is None:
            return object_renderer.DecodeFromJsonSafe(item, options)

        try:
            result = self.renderer.cache.Get(key)
        except KeyError:
            result = object_renderer.DecodeFromJsonSafe(item, options)
            self.renderer.cache.Put(key, result)

        return result


class JsonRenderer(renderer_module.BaseRenderer):
    """Render the output as a json object.

    The JSON output is designed to be streamed to a remote end - that is results
    are sent incrementally as soon as they are available. The receiver can then
    process the results as they come, rendering them to screen or GUI.

    The data is essentially a list of commands.

    Each command is a list. The first parameter is the command name, further
    parameters are the args to the command.

    Currently the following commands are supported:

    l: Reset the lexicon. Followed by a lexicon dict. Following entries will be
       decoded with this lexicon.

    m: This is a metadata, followed by a dict of various metadata.

    s: Start a new section. Followed by section name.

    f: A free format text line. Followed by format string and a list of
       parameters. Parameters are dicts encoded using the lexicon.

    t: Start a new table. Followed by Table headers. Followed by a list of lists
       (human_name, name, formatstring).

    r: A table row. Followed by a list of dicts for each row cell. Each row cell
       is encoded using the lexicon for both keys and values.

    p: A progress message. Followed by a single string which is the formatted
       message.
    """

    name = "json"

    progress_interval = 1

    # This will hold a list of JSON commands to buffer them before they are
    # written to the json file.
    data = None

    def __init__(self, output=None, **kwargs):
        super(JsonRenderer, self).__init__(**kwargs)

        # Allow the user to dump all output to a file.
        self.output = output or self.session.GetParameter("output")

        # This keeps a list of object renderers which we will use for each
        # column.
        self.object_renderers = []

        fd = None
        if self.output:
            # This overwrites the output file with a new json message.
            fd = open(self.output, "wb")

        if fd == None:
            fd = self.session.fd

        if fd == None:
            fd = sys.stdout

        self.fd = fd
        self.encoder = JsonEncoder(compression=False, renderer=self)
        self.decoder = JsonDecoder(session=self.session, renderer=self)

        # A general purpose cache for encoders and decoders.
        self.cache = utils.FastStore(100)

    def start(self, plugin_name=None, kwargs=None):
        super(JsonRenderer, self).start(plugin_name=plugin_name, kwargs=kwargs)
        self.flush()

        # Save some metadata.
        self.SendMessage(
            ["m", dict(plugin_name=plugin_name,
                       tool_name="rekall",
                       tool_version=constants.VERSION,
                       )])

        return self

    def SendMessage(self, statement):
        self.data.append(statement)

    def format(self, formatstring, *args):
        statement = ["f", self.encoder.Encode(formatstring)]
        for arg in args:
            # Just store the statement in the output.
            statement.append(self.encoder.Encode(arg))

        self.SendMessage(statement)

    def section(self, name=None, **kwargs):
        kwargs["name"] = name
        self.SendMessage(["s", self.encoder.Encode(kwargs)])

    def report_error(self, message):
        self.SendMessage(["e", message])

    def table_header(self, columns=None, **kwargs):
        # TODO: Remove this when all calls are done with kwargs.
        kwargs["columns"] = columns
        self.object_renderers = [None] * len(columns)
        for i, column in enumerate(columns):
            if isinstance(column, dict):
                self.object_renderers[i] = column.get("type")
            else:
                self.object_renderers[i] = None

        self.SendMessage(["t", kwargs])

    def table_row(self, *args, **kwargs):
        result = []
        for i, arg in enumerate(args):
            result.append(self.encoder.Encode(
                arg, type=self.object_renderers[i], **kwargs))

        self.SendMessage(["r", result])

    def write_data_stream(self):
        if self.data:
            # Just dump out the json object.
            self.fd.write(json.dumps(self.data, separators=(',', ':')))
            self.fd.flush()

    def flush(self):
        self.write_data_stream()
        self.encoder.flush()

        # We store the data here.
        self.data = []

        # NOTE: The lexicon will continue to be modified, but will be sent as
        # part of the first statement.
        self.SendMessage(["l", self.encoder.GetLexicon()])

    def RenderProgress(self, message=" %(spinner)s", *args, **kwargs):
        if super(JsonRenderer, self).RenderProgress(**kwargs):
            self.SendMessage(["p", message, args, kwargs])

# Layout-preserving parser/manipulator/writer for Visual Studio 2010 projects

# Copyright (c) 2013 Mark A. Tsuchida
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.


"""
Layout-preserving parser/manipulator/writer for Visual Studio 2010 projects

Usage example 1 (input only):
    import vcxproj

    @vcxproj.coroutine
    def print_project_guid():
        while True:
            action, params = yield
            if action == "start_elem" and params["name"] == "ProjectGuid":
                action, params = yield
                assert action == "chars"
                print("Project GUID is", params["content"]

    vcxproj.check_file("myproject.vcxproj", print_project_guid)

Usage example 2 (input and output):
    import vcxproj

    @vcxproj.coroutine
    def remove_warning_level(target)
        while True:
            action, params = yield
            if action == "start_elem" and params["name"] == "WarningLevel":
                action, params = yield
                assert action == "chars"

                action, params = yield
                assert action == "end_elem"
                assert params["name"] == "WarningLevel"

                continue
            target.send((action, params))

    vcxproj.filter_file("myproject.vcxproj", remove_warning_level)

Notes:
- Tested with Python 3.3.

- Input is assumed to conform to the normal layout generated by Visual Studio.
  Output is written in this layout, preserving element and attribute ordering.

- Currently, always writes CRLF newlines and in utf-8-sig (i.e. with BOM),
  which is the usual format produced by Visual Studio.
"""


from collections import OrderedDict
from xml.parsers import expat
from xml.sax.saxutils import quoteattr
import codecs
import io
import sys
dict = OrderedDict  # Preserve order everywhere


def coroutine(genfunc):
    """Decorator for toplevel coroutines.

    Filter and checker coroutines should be defined with this decorator.

    Automatically primes coroutiens by calling next().
    """
    def wrapped(*args, **kwargs):
        generator = genfunc(*args, **kwargs)
        next(generator)
        return generator
    return wrapped


def subcoroutine(genfunc):
    """Decorator for subcoroutines.

    Use this for coroutines to be called via 'yield from'.
    """
    return genfunc


@coroutine
def null_sink():
    while True:
        yield


@subcoroutine
def skip_to(target, name=None, attr_test=None):
    """A simple but versatile primitive for constructing filters.

    name - element name
    attr_test - callable taking attrs dict and returning bool

    Skips (forwarding items to target) to the next element at the current
    nesting level that matches name and passes attr_test. If matching element
    is found, returns (True, "start_elem", params), before forwarding the
    start_elem item to target. If no matching element is found within the
    current element, returns (False, "end_elem", params), corresponding to the
    closing tag of the current element, but before forwarding the end_elem item
    to target.

    If both name and attr_test are None, does not match any element and always
    returns (False, "end_elem", params). Otherwise, if one of name or attr_test
    is None, only the other criterion is used for matching an element.
    """
    if target is None:
        target = null_sink()
    element_stack = list()
    match_nothing = name is None and attr_test is None
    while True:
        action, params = yield
        if action == "start_elem":
            if not match_nothing and not element_stack:
                if name is None or params["name"] == name:
                    if attr_test is None or attr_test(params["attrs"]):
                        return True, action, params
            element_stack.append(params["name"])
        elif action == "end_elem":
            if not element_stack:
                return False, action, params
            e = element_stack.pop(); assert e == params["name"]
        target.send((action, params))


@subcoroutine
def set_content(target, name, content):
    """Set element content or insert element with content.

    A subcoroutine for constructing filters, to be used together with
    (following) skip_to() to reach the desired enclosing element.

    Forwards items to target. If, at the current nesting level, an element with
    the given name is encountered, its content is replaced with the given
    content. Otherwise, a new element with the given name and content is
    inserted at the end of the current (enclosing) element. Returns
    ("end_elem", params), corresponding to the closing tag of the current
    element, but before forwarding the end_elem to target.
    """
    found, action, params = yield from skip_to(target, name)
    if found:
        attrs = params["attrs"]
        action, params = yield
        assert action == "chars"
        _, action, params = yield from skip_to(target)
        send_element(target, name, attrs, content)

        found, action, params = yield from skip_to(target, name)
        assert not found, "Duplicate element: " + name
    else:
        send_element(target, name, dict(), content)
    return action, params


def send_element(target, name, attrs, content=None):
    """Convenience function to send a simple element."""
    target.send(("start_elem", dict([("name", name),
                                     ("attrs", attrs)])))
    if content is not None:
        target.send(("chars", dict(content=content)))
    target.send(("end_elem", dict(name=name)))


def check_file(input_filename, genchecker):
    """Read and check (or otherwise process) a project file.

    genchecker - callable taking no args and returning checker coroutine

    The checker coroutine receives parsed items via 'yield'.
    """
    if genchecker is None:
        genchecker = null_sink

    pipeline = geninput(genchecker())
    process_file(input_filename, pipeline)


def filter_file(input_filename, genfilter, output_filename):
    """Read, process, and rewrite a project file.

    genfilter - callable taking output coroutine and returning filter coroutine

    The filter coroutine receives parsed items via 'yield' and sould send items
    to the output coroutine using the latter's send() method.
    """
    if genfilter is None:
        genfilter = lambda x: x  # No filter

    # Buffer all output to allow for in-place rewriting
    output_stream = io.StringIO()
    pipeline = geninput(genfilter(genoutput(output_stream)))
    process_file(input_filename, pipeline)

    with codecs.open(output_filename, "w", "utf-8-sig") as file:
        file.write(output_stream.getvalue())


def geninput(target):
    """Add the standard input processing pipeline.

    Given target, a coroutine that receives parsed items, return a coroutine
    that recieves raw XML events.
    """
    return filter_chars(target)


def genoutput(writer):
    """Construct the standard output processing pipeline.

    Given writer, a writeable file object, return a coroutine that receives
    parsed items and writes XML output to writer.
    """
    return to_lines(compute_indent(to_strings(line_writer(writer))))


def process_file(filename, pipeline):
    parser = ExpatParser(pipeline)
    try:
        parser.parse_filename(filename)
    except expat.ExpatError as err:
        print("Error:", expat.errors.messages[err.code], file=sys.stderr)


def xml_indent(n):
    return "  " * n


def xml_tag_open_elem(name, attrs):
    return "<{}{}>".format(name, xml_attrs(attrs))


def xml_tag_empty_elem(name, attrs):
    return "<{}{} />".format(name, xml_attrs(attrs))


def xml_tag_close_elem(name):
    return "</{}>".format(name)


def xml_attrs(attrs):
    return "".join(" {}={}".format(name, quoteattr(value))
                   for name, value in attrs.items())


@coroutine
def logger(target, prefix="", writer=print):
    """A pass-through coroutine that prints items.

    For debugging various stages of a coroutine pipeline.
    """
    while True:
        item = yield
        writer(prefix, item)
        target.send(item)


@coroutine
def item_logger(target, prefix="", writer=print):
    """A pass-through logger for parsed items."""
    indent = 0
    while True:
        action, params = yield
        if action == "start_elem":
            writer(prefix + "  " * indent, "start[{}]:".format(params["name"]),
                   ", ".join("{}={}".format(key, repr(val))
                             for key, val in params["attrs"].items()))
            indent += 1
        elif action == "end_elem":
            indent -= 1
            writer(prefix + "  " * indent, "end[{}]".format(params["name"]))
        elif action == "chars":
            writer(prefix + "  " * indent, "chars:", repr(params["content"]))
        elif action == "noop":
            writer(prefix + "  " * indent, "noop")
        else:
            assert False, "Unexpected action"
        target.send((action, params))


@coroutine
def line_writer(writer, newline="\r\n"):
    """Sink coroutine; writes strings as lines to writer.
    
    writer: writable file object
    input = string

    No newline is added at the end of the output.
    """
    line = yield
    writer.write(line)
    while True:
        line = yield
        writer.write(newline + line)


@coroutine
def to_strings(target):
    """Turn element-line items into strings.

    input = (indent_count, line_type, param_dict)
    output = string
    """
    target.send('<?xml version="1.0" encoding="utf-8"?>')
    while True:
        indent, action, params = yield
        if action == "start_elem_line":
            target.send(xml_indent(indent) +
                        xml_tag_open_elem(**params))
        elif action == "empty_elem_line":
            target.send(xml_indent(indent) +
                        xml_tag_empty_elem(**params))
        elif action == "content_elem_line":
            element_str = (xml_indent(indent) +
                           xml_tag_open_elem(name=params["name"],
                                             attrs=params["attrs"]) +
                           params["content"] +
                           xml_tag_close_elem(name=params["name"]))
            # Content may contain newlines
            for line in element_str.split("\n"):
                target.send(line)
        elif action == "end_elem_line":
            target.send(xml_indent(indent) +
                        xml_tag_close_elem(**params))


@coroutine
def compute_indent(target):
    """Add indent count to line items.

    input = (line_type, param_dict)
    output = (indent_count, line_type, param_dict)
    """
    indent = 0
    try:
        while True:
            action, params = yield
            if action == "end_elem_line":
                indent -= 1
            target.send((indent, action, params))
            if action == "start_elem_line":
                indent += 1
    except GeneratorExit:
        target.close()


@coroutine
def to_lines(target):
    """Convert XML event stream to line stream.

    input = (event_type, param_dict)
    output = (line_type, param_dict)
    """
    action, params = yield
    while True:
        if action == "start_elem":
            action, params = (yield from
                              to_lines_post_start_elem(target, **params))
            continue

        if action == "end_elem":
            target.send(("end_elem_line", params))
            action, params = yield
            continue

        action, params = yield


@subcoroutine
def to_lines_post_start_elem(target, **start_elem):
    """Sub-coroutine for to_lines().

    Returns the next (peeked) (action, params).
    """

    action, params = yield

    if action == "end_elem":
        assert params["name"] == start_elem["name"]
        target.send(("empty_elem_line", start_elem))
        return (yield)

    if action == "chars":
        return (yield from to_lines_elem_chars(target, start_elem, params))

    target.send(("start_elem_line", start_elem))
    return action, params


@subcoroutine
def to_lines_elem_chars(target, start_elem, chars):
    """Sub-coroutine for to_lines_post_start_elem().
    
    Returns the next (peeked) (action, params).
    """
    action, params = "chars", chars
    content = ""
    while action == "chars":
        content += params["content"]
        action, params = yield

    if action == "end_elem":
        assert params["name"] == start_elem["name"]
        target.send(("content_elem_line",
                     dict(name=start_elem["name"],
                          attrs=start_elem["attrs"],
                          content=content)))
        return (yield)

    assert not content.strip()
    target.send(("start_elem_line", start_elem))
    return action, params


@coroutine
def filter_chars(target):
    """Remove ignorable "chars" actions.

    Also consolidates "chars" with content into a single item.
    """
    # Whitespace can be ignored before a start_elem and after an end_elem.
    last_action = None
    content = None  # Not None after a start_elem
    has_content = False  # True after a start_elem followed by a chars.
    while True:
        action, params = yield
        if action == "chars":
            if content is not None:
                content += params["content"]
                has_content = True
            continue
        if action == "start_elem":
            content = ""
            has_content = False
        elif action == "end_elem":
            if content is not None and has_content:
                if "\n" in content and not content.strip():
                    # We have an empty element list.
                    # Preserve separate open-close tags.
                    target.send(("noop", dict()))
                else:
                    target.send(("chars", dict(content=content)))
            content = None
            has_content = False
        target.send((action, params))


class ExpatParser:
    """Wrapper for an Expat parser; acts as pipeline source."""

    def __init__(self, target):
        self.target = target
        self.parser = self._setup_parser()

    def _setup_parser(self):
        parser = expat.ParserCreate()

        parser.ordered_attributes = True
        parser.specified_attributes = True

        parser.StartElementHandler = self.on_start_element
        parser.EndElementHandler = self.on_end_element
        parser.CharacterDataHandler = self.on_characters

        return parser

    def parse_filename(self, filename):
        with open(filename, "rb") as input:
            self.parse_file(input)

    def parse_file(self, binary_stream):
        self.parser.ParseFile(binary_stream)

    def on_start_element(self, name, attrs):
        # attrs is [name0, value0, name1, value1, ...]
        iattrs = iter(attrs)
        attr_items = zip(iattrs, iattrs)
        attrs = OrderedDict(attr_items)
        self.target.send(("start_elem", dict(name=name, attrs=attrs)))

    def on_end_element(self, name):
        self.target.send(("end_elem", dict(name=name)))

    def on_characters(self, content):
        self.target.send(("chars", dict(content=content)))


def test():
    """Parse and rewrite, unmodified, the specified file.

    Print the parsed items to stdout.
    """
    input_filename = sys.argv[1]
    output_filename = input_filename
    if len(sys.argv) > 2:
        output_filename = sys.argv[2]

    genfilter = lambda target: item_logger(target)
    filter_file(input_filename, genfilter, output_filename)


if __name__ == "__main__":
    test()

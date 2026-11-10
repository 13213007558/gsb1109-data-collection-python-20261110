from __future__ import absolute_import

import re
from collections import defaultdict

from ._form_controls import HTMLForm, Label
from ._request import Request
from .polyglot import urljoin, is_string, as_unicode


class SkipControl(ValueError):
    pass


def normalize_line_endings(text):
    return re.sub(as_unicode(r"(?:(?<!\r)\n)|(?:\r(?!\n))"), u"\r\n", text)


def label_text(elem):
    ans = []
    if elem.text:
        ans.append(elem.text)
    for child in elem:
        if child.tail:
            ans.append(child.tail)
    return ''.join(ans)


# elements whose visible text is not part of the surrounding form's text
# (their contents are data, not labels)
_NON_LABEL_TEXT_TAGS = frozenset((
    'script', 'style', 'option', 'optgroup', 'textarea', 'title'))

# table cell tags, given sibling-aware treatment below
_CELL_TAGS = frozenset(('td', 'th'))


def _compress_ws(text):
    return re.sub(r'\s+', ' ', text or '').strip()


def visible_text(elem):
    """Return the visible text contained in elem, in document order.

    Text inside elements such as <script>, <style>, <option> and <textarea>
    is excluded, since it is not an on-screen caption for another control.

    """
    ans = []

    def collect(node):
        if node.text and node.tag.lower() not in _NON_LABEL_TEXT_TAGS:
            ans.append(node.text)
        for child in node:
            if not is_string(child.tag):
                continue
            if child.tag.lower() in _NON_LABEL_TEXT_TAGS:
                # keep any text that follows the element (its .tail)
                if child.tail:
                    ans.append(child.tail)
                continue
            collect(child)
            if child.tail:
                ans.append(child.tail)

    collect(elem)
    return ''.join(ans)


def _enclosing(elem, parent_map, tags):
    node = elem
    while node is not None:
        if is_string(node.tag) and node.tag.lower() in tags:
            return node
        node = parent_map.get(node)


def table_cell_label(elem, parent_map):
    """Return the caption for a control laid out in a table cell.

    Uses the visible text of the control's own <td>/<th> when present,
    otherwise the text of the nearest non-empty preceding cell in the same
    row -- the classic "caption in the left cell, field in the right cell"
    layout.  Returns None when no caption is available.

    Suitable for use as a callable label source.

    """
    cell = _enclosing(elem, parent_map, _CELL_TAGS)
    if cell is None:
        return None
    text = _compress_ws(visible_text(cell))
    if text:
        return text
    row = parent_map.get(cell)
    if row is None or not is_string(row.tag) or row.tag.lower() != 'tr':
        return None
    cells = [child for child in row
             if is_string(child.tag) and child.tag.lower() in _CELL_TAGS]
    try:
        index = next(i for i, child in enumerate(cells) if child is cell)
    except StopIteration:
        return None
    for sibling in reversed(cells[:index]):
        text = _compress_ws(visible_text(sibling))
        if text:
            return text
    return None


def auxiliary_label_texts(elem, parent_map, label_sources):
    """Collect auxiliary label texts for control element elem.

    Each entry in label_sources is one of:

    - a (tag, None) pair: visible text of elem itself or an ancestor of that
      tag; for table cells ("td"/"th") the nearest preceding labelled cell in
      the same row is also considered;
    - a (tag, attribute) pair: value of that attribute on elem itself or an
      ancestor of that tag;
    - a callable taking (elem, parent_map) and returning a string, an iterable
      of strings, or None for fully custom sources.

    Results are returned outermost-first so that the closest association wins
    when callers take the last one.

    """
    chain = []
    node = elem
    while node is not None:
        chain.append(node)
        node = parent_map.get(node)

    texts = []

    def add(text):
        if text and text not in texts:
            texts.append(text)

    for source in label_sources:
        if callable(source):
            produced = source(elem, parent_map)
            if is_string(produced):
                add(produced)
            elif produced:
                for piece in produced:
                    add(piece)
            continue

        tag, attr = source
        if attr is None and tag in _CELL_TAGS:
            add(table_cell_label(elem, parent_map))
            continue

        for node in reversed(chain):
            if not is_string(node.tag) or node.tag.lower() != tag:
                continue
            if attr is None:
                add(visible_text(node))
            else:
                add(node.get(attr))
    return texts


def parse_control(elem, parent_of, default_type='text',
                  parent_map=None, label_sources=()):
    attrs = dict(elem.attrib)
    label_elem = parent_of(elem, 'label')
    if label_elem is not None:
        lt = label_text(label_elem)
        if lt:
            attrs["__label"] = lt
    if label_sources and parent_map is not None:
        auxiliary = auxiliary_label_texts(elem, parent_map, label_sources)
        if auxiliary:
            attrs["__aux_labels"] = auxiliary
    ctype = attrs.get('type') or default_type
    return ctype, attrs.get('name'), attrs


def parse_input(elem, parent_of, *a, **kw):
    return parse_control(elem, parent_of, **kw)


def parse_button(elem, parent_of, *a, **kw):
    ctype, name, attrs = parse_control(
        elem, parent_of, default_type='submit', **kw)
    ctype += 'button'
    return ctype, name, attrs


def parse_option(elem, parent_of, attrs_map, **kw):
    ctype, name, attrs = parse_control(elem, parent_of, **kw)
    og = parent_of(elem, 'optgroup')
    contents = (elem.text or '').strip()
    attrs['contents'] = contents
    attrs['value'] = attrs.get('value', contents)
    attrs['label'] = attrs.get('label', contents)
    if og is not None and og.get('disabled') is not None:
        attrs['disabled'] = 'disabled'
    sel = parent_of(elem, 'select')
    if sel is None:
        raise SkipControl()
    attrs['__select'] = sel = attrs_map[sel]['__select']
    return 'select', sel.get('name'), attrs


def parse_textarea(elem, parent_of, *a, **kw):
    ctype, name, attrs = parse_control(elem, parent_of, **kw)
    ctype = 'textarea'
    attrs['value'] = normalize_line_endings(elem.text or u'')
    return ctype, name, attrs


def parse_select(elem, parent_of, *a, **kw):
    ctype, name, attrs = parse_control(elem, parent_of, **kw)
    ctype = 'select'
    return ctype, name, {'__select': attrs}


def normalize_label_sources(label_sources):
    """Normalize the label_sources argument to parse_forms.

    Accepts strings ("td"), (tag, attr) tuples/pairs, callables taking
    (elem, parent_map), or an iterable mixing them.  A bare tag name means
    "the visible text inside that element"; a (tag, attribute) pair means
    "the value of that attribute"; a callable is a fully custom source.
    Returns a tuple with lower-cased tag names and duplicates removed;
    callables are kept verbatim.

    """
    if not label_sources:
        return ()
    if is_string(label_sources) or callable(label_sources):
        label_sources = (label_sources,)
    result = []
    for source in label_sources:
        if callable(source):
            normalized = source
        elif is_string(source):
            normalized = (source.lower(), None)
        else:
            tag, attr = source
            if attr is not None:
                attr = attr.lower()
            normalized = (tag.lower(), attr)
        if normalized not in result:
            result.append(normalized)
    return tuple(result)


def parse_forms(root, base_url, request_class=None, select_default=False,
                encoding=None, label_sources=()):
    if request_class is None:
        request_class = Request
    label_sources = normalize_label_sources(label_sources)
    global_form = HTMLForm(base_url, encoding=encoding,
                           label_sources=label_sources)
    forms, labels = [], []
    form_elems = []
    form_id_map = {}
    all_elems = tuple(
        e for e in root.iter('*') if is_string(e.tag))
    parent_map = {c: p for p in all_elems for c in p}
    id_to_labels = defaultdict(list)
    for e in all_elems:
        q = e.tag.lower()
        if q == 'form':
            form_elems.append(e)
            fid = e.get('id')
            if fid:
                form_id_map[fid] = e
        elif q == 'label':
            for_id = e.get('for')
            if for_id is not None:
                label = Label(label_text(e), for_id)
                labels.append(label)
                id_to_labels[for_id].append(label)
        elif q == 'base':
            base_url = e.get('href') or base_url

    def parent_of(elem, parent_name):
        q = elem
        while True:
            q = parent_map.get(q)
            if q is None:
                return
            if q.tag.lower() == parent_name:
                return q

    forms_map = {}
    for form_elem in form_elems:
        name = form_elem.get('name') or None
        action = form_elem.get('action') or None
        method = form_elem.get('method') or 'GET'
        enctype = form_elem.get(
            'enctype') or "application/x-www-form-urlencoded"
        if action:
            action = urljoin(base_url, action)
        else:
            action = base_url
        form = HTMLForm(action, method, enctype, name, form_elem.attrib,
                        request_class, forms, labels, id_to_labels,
                        encoding=encoding, label_sources=label_sources)
        forms_map[form_elem] = form
        forms.append(form)

    attrs_map = {}
    control_names = {
        'option': parse_option,
        'button': parse_button,
        'input': parse_input,
        'textarea': parse_textarea,
        'select': parse_select,
    }

    for i, elem in enumerate(all_elems):
        q = elem.tag.lower()
        cfunc = control_names.get(q)
        if cfunc is not None:
            fid = elem.get('form')
            if fid and fid in form_id_map:
                form_elem = form_id_map[fid]
            else:
                form_elem = parent_of(elem, 'form')
            form = forms_map.get(form_elem, global_form)
            try:
                control_type, control_name, attrs = cfunc(
                    elem, parent_of, attrs_map,
                    parent_map=parent_map, label_sources=label_sources)
            except SkipControl:
                continue
            attrs_map[elem] = attrs
            form.new_control(
                control_type,
                control_name,
                attrs,
                index=i * 10,
                select_default=select_default)

    for form in forms:
        form.fixup()
    global_form.fixup()

    return forms, global_form

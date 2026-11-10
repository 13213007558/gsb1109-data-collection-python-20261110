"""Pre-submission form validation.

This module provides an explicit, side-effect free validation entry point
for HTMLForm instances: it never sends a request and never modifies control
values, so a program may inspect the result and still submit the form in the
usual way (e.g. HTMLForm.click()) regardless of the outcome.

The checks implemented here cover a practical subset of HTML form
constraints:

- required (text-like controls, file controls, radio groups, checkboxes
  and select controls)
- min / max / step for numeric controls (input type=number and type=range)
- maxlength / minlength for text-like controls

Constraints that are not implemented (for example the pattern attribute,
email/URL syntax, or date/time min/max/step) are reported as "unchecked"
rather than silently assumed to hold.  This module does not attempt to be
equivalent to a full browser constraint-validation implementation.

Numeric comparisons and step arithmetic use decimal.Decimal, so decimal
steps such as step="0.1" are not misjudged by binary floating point error.
"""
from __future__ import absolute_import

from decimal import Decimal, InvalidOperation

from ._form_controls import FileControl, ListControl
from .polyglot import unicode_type

# Field statuses
VALID = 'valid'
INVALID = 'invalid'
UNCHECKED = 'unchecked'

# Control types that never take part in validation.
_SKIP_TYPES = frozenset([
    'hidden', 'submit', 'submitbutton', 'image', 'button', 'buttonbutton',
    'reset', 'resetbutton',
])

# Text-like types for which maxlength/minlength apply.
_LENGTH_TYPES = frozenset([
    'text', 'password', 'textarea', 'search', 'email', 'url', 'tel',
])

# Types whose value format this module does not check.
_FORMAT_UNCHECKED_TYPES = frozenset(['email', 'url', 'tel', 'color'])

# Date/time types: value format and min/max/step are not checked.
_DATE_TYPES = frozenset(['date', 'datetime-local', 'month', 'week', 'time'])

_NUMERIC_TYPES = frozenset(['number', 'range'])


class FieldResult:
    """Validation result for a single control (or radio/checkbox group).

    :ivar name: control name (may be None for unnamed controls)
    :ivar type: control type string, e.g. "text", "number", "radio"
    :ivar problems: list of human-readable constraint violation messages
    :ivar unsupported: list of constraint descriptions that could not be
        checked by this implementation
    :ivar control: the Control instance this result refers to

    """

    def __init__(self, control):
        self.control = control
        self.name = control.name
        self.type = control.type
        self.problems = []
        self.unsupported = []

    @property
    def status(self):
        """One of VALID, INVALID or UNCHECKED.

        INVALID if any constraint was violated, UNCHECKED if no violation
        was found but some constraint could not be checked, VALID otherwise.

        """
        if self.problems:
            return INVALID
        if self.unsupported:
            return UNCHECKED
        return VALID

    @property
    def ok(self):
        """True if no constraint violation was found (status != INVALID)."""
        return not self.problems

    def __repr__(self):
        return '<FieldResult(%s=%r, status=%s, problems=%r, unsupported=%r)>' % (
            self.type, self.name, self.status, self.problems, self.unsupported)


class FormValidationResult:
    """Aggregated result of validating a whole form.

    :ivar fields: list of FieldResult, one per validated control

    """

    def __init__(self, fields):
        self.fields = list(fields)

    @property
    def valid(self):
        """True if no field has a constraint violation.

        Note that fields with unchecked constraints do not make the form
        invalid; inspect the unchecked_fields list if you need strictness.

        """
        return not any(field.problems for field in self.fields)

    @property
    def invalid_fields(self):
        return [field for field in self.fields if field.status == INVALID]

    @property
    def unchecked_fields(self):
        return [field for field in self.fields if field.status == UNCHECKED]

    @property
    def valid_fields(self):
        return [field for field in self.fields if field.status == VALID]

    def __bool__(self):
        return self.valid

    def __iter__(self):
        return iter(self.fields)

    def __len__(self):
        return len(self.fields)

    def __str__(self):
        lines = []
        for field in self.fields:
            for problem in field.problems:
                lines.append('%s: %s' % (field.name, problem))
        if not lines:
            return 'form is valid'
        return '\n'.join(lines)

    def __repr__(self):
        return '<FormValidationResult(valid=%s, fields=%d)>' % (
            self.valid, len(self.fields))


def _parse_decimal(text):
    """Parse text as Decimal, returning None if not a valid number."""
    if text is None:
        return None
    try:
        return Decimal(unicode_type(text).strip())
    except InvalidOperation:
        return None


def _parse_int(text):
    if text is None:
        return None
    try:
        return int(unicode_type(text).strip())
    except (TypeError, ValueError):
        return None


def _validate_scalar(control, result):
    attrs = control.attrs
    ctype = control.type
    value = control.value

    if 'pattern' in attrs:
        result.unsupported.append(
            "pattern attribute %r is not checked" % attrs['pattern'])
    if ctype in _FORMAT_UNCHECKED_TYPES:
        result.unsupported.append(
            "value format of input type %r is not checked" % ctype)
    if ctype in _DATE_TYPES:
        result.unsupported.append(
            "value format and min/max/step of input type %r are not checked"
            % ctype)
        # required still applies to date controls
        if 'required' in attrs and not control.readonly and not value:
            result.problems.append('required field is empty')
        return

    empty = value is None or value == ''
    if 'required' in attrs and not control.readonly and empty:
        result.problems.append('required field is empty')
        return  # further checks are meaningless on an empty value
    if empty:
        return

    if ctype in _LENGTH_TYPES:
        maxlength = _parse_int(attrs.get('maxlength'))
        if maxlength is not None and len(value) > maxlength:
            result.problems.append(
                'value length %d exceeds maxlength %d' %
                (len(value), maxlength))
        minlength = _parse_int(attrs.get('minlength'))
        if minlength is not None and len(value) < minlength:
            result.problems.append(
                'value length %d is below minlength %d' %
                (len(value), minlength))

    if ctype in _NUMERIC_TYPES:
        _validate_numeric(control, result, value)


def _validate_numeric(control, result, value):
    attrs = control.attrs
    number = _parse_decimal(value)
    if number is None:
        result.problems.append('value %r is not a valid number' % (value,))
        return
    minimum = _parse_decimal(attrs.get('min'))
    maximum = _parse_decimal(attrs.get('max'))
    if minimum is not None and number < minimum:
        result.problems.append(
            'value %s is below minimum %s' % (number, minimum))
    if maximum is not None and number > maximum:
        result.problems.append(
            'value %s exceeds maximum %s' % (number, maximum))
    step_attr = attrs.get('step')
    if step_attr is None:
        step = Decimal(1)
    elif step_attr.strip().lower() == 'any':
        return  # no step constraint
    else:
        step = _parse_decimal(step_attr)
        if step is None or step <= 0:
            return  # invalid step attribute: browsers ignore it
    # Step base: the min attribute if present and valid, else 0 (matches
    # the HTML definition of step base for number/range controls).
    base = minimum if minimum is not None else Decimal(0)
    # Decimal arithmetic is exact, so decimal steps such as 0.1 are not
    # misjudged by binary floating point error.
    if (number - base) % step != 0:
        result.problems.append(
            'value %s is not aligned to step %s (base %s)' %
            (number, step, base))


def _validate_file(control, result):
    if 'accept' in control.attrs:
        result.unsupported.append(
            "accept attribute %r is not checked" % control.attrs['accept'])
    if 'required' in control.attrs and not control._upload_data:
        result.problems.append('required file field has no file')


def _validate_list(control, result):
    if control.type == 'radio':
        # A radio group is required if any of its inputs has the required
        # attribute; the group as a whole must then have a selection.
        required = any('required' in item.attrs for item in control.items)
        if required and not control.value:
            result.problems.append(
                'required radio group has no selected item')
    elif control.type == 'checkbox':
        # required on a checkbox means that specific checkbox must be
        # checked.
        for item in control.items:
            if ('required' in item.attrs and not item.disabled
                    and not item.selected):
                label = item.attrs.get('value', item.name)
                result.problems.append(
                    'required checkbox %r is not checked' % (label,))
    elif control.type == 'select':
        if 'required' in control.attrs:
            # A required select needs a selected option with a non-empty
            # value.
            if not any(item.name != '' for item in control.items
                       if item.selected and not item.disabled):
                result.problems.append(
                    'required select has no selected option with a value')


def validate_form(form):
    """Validate an HTMLForm without submitting it.

    Returns a FormValidationResult with one FieldResult per validated
    control.  Disabled controls, and control types that carry no
    constraints (hidden, submit, button, image, reset), are skipped.

    This function performs no network I/O and does not modify any control
    values; the form may still be submitted afterwards in the usual way.

    """
    fields = []
    for control in form.controls:
        if control.disabled or control.type in _SKIP_TYPES:
            continue
        result = FieldResult(control)
        if isinstance(control, ListControl):
            _validate_list(control, result)
        elif isinstance(control, FileControl):
            _validate_file(control, result)
        else:
            _validate_scalar(control, result)
        fields.append(result)
    return FormValidationResult(fields)

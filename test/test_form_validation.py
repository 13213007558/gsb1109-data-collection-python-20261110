#!/usr/bin/env python
# -*- coding: utf-8 -*-

import unittest
from io import BytesIO

import mechanize
from mechanize._form_validation import (INVALID, UNCHECKED, VALID,
                                        validate_form)
from mechanize._html import content_parser
import mechanize._form as _form


def first_form(html, base_uri="http://example.com/"):
    if isinstance(html, str):
        html = html.encode('utf-8')
    root = content_parser(BytesIO(html))
    forms, _global_form = _form.parse_forms(root, base_uri)
    return forms[0]


def field_by_name(result, name):
    for field in result.fields:
        if field.name == name:
            return field
    raise AssertionError("no field named %r in %r" % (name, result.fields))


class RequiredTests(unittest.TestCase):

    def test_required_text_empty_and_filled(self):
        form = first_form(
            b'<form><input type=text name=t required></form>')
        result = form.validate()
        field = field_by_name(result, 't')
        self.assertEqual(field.status, INVALID)
        self.assertFalse(result.valid)

        form['t'] = 'hello'
        result = form.validate()
        self.assertEqual(field_by_name(result, 't').status, VALID)
        self.assertTrue(result.valid)
        self.assertTrue(bool(result))

    def test_optional_empty_text_is_valid(self):
        form = first_form(b'<form><input type=text name=t></form>')
        self.assertTrue(form.validate().valid)

    def test_required_textarea(self):
        form = first_form(
            b'<form><textarea name=ta required></textarea></form>')
        self.assertEqual(field_by_name(form.validate(), 'ta').status, INVALID)
        form['ta'] = 'some text'
        self.assertEqual(field_by_name(form.validate(), 'ta').status, VALID)

    def test_required_file(self):
        form = first_form(
            b'<form method=POST enctype="multipart/form-data">'
            b'<input type=file name=f required></form>')
        result = form.validate()
        self.assertEqual(field_by_name(result, 'f').status, INVALID)
        form.add_file(BytesIO(b'data'), filename='x.txt', name='f')
        self.assertEqual(field_by_name(form.validate(), 'f').status, VALID)

    def test_disabled_and_hidden_controls_are_skipped(self):
        form = first_form(
            b'<form><input type=text name=d required disabled>'
            b'<input type=hidden name=h required>'
            b'<input type=submit name=s></form>')
        result = form.validate()
        self.assertEqual(result.fields, [])
        self.assertTrue(result.valid)


class NumericTests(unittest.TestCase):

    def test_min_max(self):
        form = first_form(
            b'<form><input type=number name=n min=1 max=10></form>')
        form['n'] = '0'
        self.assertEqual(field_by_name(form.validate(), 'n').status, INVALID)
        form['n'] = '11'
        self.assertEqual(field_by_name(form.validate(), 'n').status, INVALID)
        form['n'] = '5'
        self.assertEqual(field_by_name(form.validate(), 'n').status, VALID)
        form['n'] = '1'
        self.assertEqual(field_by_name(form.validate(), 'n').status, VALID)
        form['n'] = '10'
        self.assertEqual(field_by_name(form.validate(), 'n').status, VALID)

    def test_non_numeric_value(self):
        form = first_form(b'<form><input type=number name=n></form>')
        form['n'] = 'abc'
        field = field_by_name(form.validate(), 'n')
        self.assertEqual(field.status, INVALID)
        self.assertIn('not a valid number', field.problems[0])

    def test_decimal_step_not_misjudged_by_float_error(self):
        # 0.3 / 0.1 == 2.9999999999999996 in binary floating point, so a
        # float-based check would wrongly reject 0.3 here.
        form = first_form(
            b'<form><input type=number name=n step=0.1></form>')
        form['n'] = '0.3'
        self.assertEqual(field_by_name(form.validate(), 'n').status, VALID)
        form['n'] = '0.35'
        self.assertEqual(field_by_name(form.validate(), 'n').status, INVALID)

    def test_step_base_is_min(self):
        form = first_form(
            b'<form><input type=number name=n min=0.05 step=0.1></form>')
        form['n'] = '0.25'  # 0.05 + 2 * 0.1
        self.assertEqual(field_by_name(form.validate(), 'n').status, VALID)
        form['n'] = '0.3'  # not on the 0.05-based grid
        self.assertEqual(field_by_name(form.validate(), 'n').status, INVALID)

    def test_default_step_is_one(self):
        form = first_form(b'<form><input type=number name=n></form>')
        form['n'] = '3'
        self.assertEqual(field_by_name(form.validate(), 'n').status, VALID)
        form['n'] = '3.5'
        self.assertEqual(field_by_name(form.validate(), 'n').status, INVALID)

    def test_step_any_disables_step_check(self):
        form = first_form(
            b'<form><input type=number name=n step=any></form>')
        form['n'] = '3.14159'
        self.assertEqual(field_by_name(form.validate(), 'n').status, VALID)

    def test_range(self):
        form = first_form(
            b'<form><input type=range name=r min=0 max=1 step=0.25></form>')
        form['r'] = '0.75'
        self.assertEqual(field_by_name(form.validate(), 'r').status, VALID)
        form['r'] = '0.8'
        self.assertEqual(field_by_name(form.validate(), 'r').status, INVALID)

    def test_required_number_empty(self):
        form = first_form(
            b'<form><input type=number name=n required min=1></form>')
        self.assertEqual(field_by_name(form.validate(), 'n').status, INVALID)


class LengthTests(unittest.TestCase):

    def test_maxlength(self):
        form = first_form(
            b'<form><input type=text name=t maxlength=3></form>')
        form['t'] = 'abc'
        self.assertEqual(field_by_name(form.validate(), 't').status, VALID)
        form['t'] = 'abcd'
        self.assertEqual(field_by_name(form.validate(), 't').status, INVALID)

    def test_minlength(self):
        form = first_form(
            b'<form><input type=password name=p minlength=4></form>')
        form['p'] = 'abc'
        self.assertEqual(field_by_name(form.validate(), 'p').status, INVALID)
        form['p'] = 'abcd'
        self.assertEqual(field_by_name(form.validate(), 'p').status, VALID)

    def test_maxlength_textarea(self):
        form = first_form(
            b'<form><textarea name=ta maxlength=2></textarea></form>')
        form['ta'] = 'toolong'
        self.assertEqual(field_by_name(form.validate(), 'ta').status, INVALID)


class GroupConstraintTests(unittest.TestCase):

    def test_required_radio_group(self):
        form = first_form(
            b'<form>'
            b'<input type=radio name=r value=a required>'
            b'<input type=radio name=r value=b>'
            b'</form>')
        result = form.validate()
        field = field_by_name(result, 'r')
        self.assertEqual(field.status, INVALID)
        self.assertIn('radio', field.problems[0])

        form['r'] = ['a']
        self.assertEqual(field_by_name(form.validate(), 'r').status, VALID)

    def test_optional_radio_group_unselected_is_valid(self):
        form = first_form(
            b'<form>'
            b'<input type=radio name=r value=a>'
            b'<input type=radio name=r value=b>'
            b'</form>')
        self.assertEqual(field_by_name(form.validate(), 'r').status, VALID)

    def test_required_checkbox(self):
        form = first_form(
            b'<form>'
            b'<input type=checkbox name=agree value=yes required>'
            b'<input type=checkbox name=topping value=cheese>'
            b'</form>')
        result = form.validate()
        self.assertEqual(field_by_name(result, 'agree').status, INVALID)
        self.assertEqual(field_by_name(result, 'topping').status, VALID)
        self.assertFalse(result.valid)

        form['agree'] = ['yes']
        self.assertTrue(form.validate().valid)

    def test_required_select_rejects_empty_value_option(self):
        form = first_form(
            b'<form><select name=s required>'
            b'<option value="">-- choose --</option>'
            b'<option value=a>A</option>'
            b'</select></form>')
        # the empty-value placeholder option is selected by default
        self.assertEqual(field_by_name(form.validate(), 's').status, INVALID)
        form['s'] = ['a']
        self.assertEqual(field_by_name(form.validate(), 's').status, VALID)

    def test_required_multiple_select(self):
        form = first_form(
            b'<form><select name=m multiple required>'
            b'<option value=a>A</option>'
            b'<option value=b>B</option>'
            b'</select></form>')
        self.assertEqual(field_by_name(form.validate(), 'm').status, INVALID)
        form['m'] = ['a', 'b']
        self.assertEqual(field_by_name(form.validate(), 'm').status, VALID)

    def test_optional_multiple_select_unselected_is_valid(self):
        form = first_form(
            b'<form><select name=m multiple>'
            b'<option value=a>A</option>'
            b'</select></form>')
        self.assertEqual(field_by_name(form.validate(), 'm').status, VALID)


class UnsupportedConstraintTests(unittest.TestCase):

    def test_pattern_is_unchecked(self):
        form = first_form(
            b'<form><input type=text name=t pattern="[0-9]+"></form>')
        form['t'] = 'not digits'
        field = field_by_name(form.validate(), 't')
        self.assertEqual(field.status, UNCHECKED)
        self.assertTrue(field.ok)
        self.assertTrue(any('pattern' in u for u in field.unsupported))

    def test_email_format_is_unchecked(self):
        form = first_form(b'<form><input type=email name=e></form>')
        form['e'] = 'not an email address'
        field = field_by_name(form.validate(), 'e')
        self.assertEqual(field.status, UNCHECKED)
        self.assertTrue(result_ok(form.validate()))

    def test_date_constraints_are_unchecked(self):
        form = first_form(
            b'<form><input type=date name=d min="2020-01-01"></form>')
        form['d'] = 'not a date'
        field = field_by_name(form.validate(), 'd')
        self.assertEqual(field.status, UNCHECKED)

    def test_required_still_checked_on_date(self):
        form = first_form(
            b'<form><input type=date name=d required></form>')
        field = field_by_name(form.validate(), 'd')
        self.assertEqual(field.status, INVALID)

    def test_file_accept_is_unchecked(self):
        form = first_form(
            b'<form method=POST enctype="multipart/form-data">'
            b'<input type=file name=f accept="image/*"></form>')
        field = field_by_name(form.validate(), 'f')
        self.assertEqual(field.status, UNCHECKED)


def result_ok(result):
    return result.valid


class ResultApiTests(unittest.TestCase):

    def test_validate_form_function_and_exports(self):
        form = first_form(b'<form><input type=text name=t required></form>')
        result = mechanize.validate_form(form)
        self.assertIsInstance(result, mechanize.FormValidationResult)
        self.assertIsInstance(result.fields[0], mechanize.FieldResult)
        self.assertEqual(validate_form(form).valid, result.valid)

    def test_result_aggregation(self):
        form = first_form(
            b'<form>'
            b'<input type=text name=bad required>'
            b'<input type=text name=ok>'
            b'<input type=text name=maybe pattern="x">'
            b'</form>')
        result = form.validate()
        self.assertEqual(len(result), 3)
        self.assertEqual([f.name for f in result], ['bad', 'ok', 'maybe'])
        self.assertEqual([f.name for f in result.invalid_fields], ['bad'])
        self.assertEqual([f.name for f in result.unchecked_fields], ['maybe'])
        self.assertEqual([f.name for f in result.valid_fields], ['ok'])
        self.assertIn('bad', str(result))

    def test_validation_does_not_modify_values_or_block_submission(self):
        form = first_form(
            b'<form action="/submit" method=POST>'
            b'<input type=text name=t required>'
            b'<input type=number name=n step=0.1>'
            b'</form>')
        form['n'] = '0.3'
        before = (form['t'], form['n'])
        result = form.validate()
        self.assertFalse(result.valid)
        self.assertEqual((form['t'], form['n']), before)
        # the program may still submit in the usual way
        url, data, headers = form.click_request_data()
        self.assertIn('n=0.3', data)


if __name__ == '__main__':
    unittest.main()

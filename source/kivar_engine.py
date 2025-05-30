import pcbnew
import os
import csv
import hashlib
import difflib


# in any given project, ONE base may be used throughout, e.g. 'Var', for fields or as the base ('Var.Aspect')
# but this allows the choice to use, e.g. 'Variant'
#   Variant.Aspect
#   Variant(option) ... etc
# and auto-detects this project-wide selection automatically.  
# All available options listed here:
FieldIDOptions = ['Var', 'Variant', 'Config', 'Build']

# Note about field case-sensitivity:
# As long as KiCad can easily be tricked* into having multiple fields whose names only differ in casing, we
# will not allow case-insensitive field parsing/assignment.
# * Even though the symbol editor does not allow having "Var" and "VAR" at the same time, you can rename a field
#   to "VAR" and will still get the field name template "Var" presented, which you can fill with a value and KiCad
#   will even save that field to your file.

# TODO pre-sort errors and changes before returning them, ready to be used by caller. then remove sorting and
#      requirements (e.g. Key class import) in callers.

# TODO clarify rules for Aspect name (forbidden characters: "*" ".")

# TODO in field scope, accept only target fields which are no KiVar fields themselves (avoid recursion!)

# TODO clean-up wrap engine code in classes

# TODO use setdefault where useful

def pcbnew_compatibility_error():
    ver = pcbnew.GetMajorMinorPatchVersion()
    schema = ver.split('.')
    num = int(schema[0]) * 100 + int(schema[1])
    return None if num >= 799 else f'This version of KiVar requires KiCad pcbnew version 7.99 or later.\nYou are using pcbnew version {ver}.'

def fp_to_uuid(fp):
    return fp.m_Uuid.AsString()

def uuid_to_fp(board, uuid):
    return board.GetItem(pcbnew.KIID(uuid)).Cast()

def field_accepted(field_name):
    return not field_name.lower() in ['value', 'reference', 'footprint']

def set_fp_field(fp, field, value):
    if field_accepted(field): fp.SetField(field, value)

def legacy_expressions_found(fpdict):
    found = 0
    for uuid in fpdict:
        for field in fpdict[uuid][Key.FIELDS]:
            if field == 'KiVar.Rule' and fpdict[uuid][Key.FIELDS][field]: found += 1
    return found

class PasteRatio:
    OFFSET      = -42000.0
    TOLERANCE   =    100.0
    INHERIT     = -42420.0
    INHERIT_EPS =      0.1

class PasteMode:
    INVALID         = 0
    ON_IS_INHERIT   = 1
    ON_WITH_RATIO   = 2
    OFF_WAS_INHERIT = 3
    OFF_WAS_RATIO   = 4

def paste_mode_from_ratio(pratio):
    mode = PasteMode.INVALID
    if pratio is None:
        mode = PasteMode.ON_IS_INHERIT
    elif pratio <= (PasteRatio.TOLERANCE) and pratio >= (-PasteRatio.TOLERANCE):
        mode = PasteMode.ON_WITH_RATIO
    elif pratio <= (PasteRatio.INHERIT + PasteRatio.INHERIT_EPS) and pratio >= (PasteRatio.INHERIT - PasteRatio.INHERIT_EPS):
        mode = PasteMode.OFF_WAS_INHERIT
    elif pratio <= (PasteRatio.OFFSET + PasteRatio.TOLERANCE) and pratio >= (PasteRatio.OFFSET - PasteRatio.TOLERANCE):
        mode = PasteMode.OFF_WAS_RATIO
    return mode

def paste_state_from_ratio(pratio):
    mode = paste_mode_from_ratio(pratio)
    state = None
    if   mode in (PasteMode.ON_IS_INHERIT, PasteMode.ON_WITH_RATIO):   state = True
    elif mode in (PasteMode.OFF_WAS_INHERIT, PasteMode.OFF_WAS_RATIO): state = False
    return state

def paste_ratio_text(pratio):
    if pratio is None: s = 'Inherited'
    else:              s = f'{round(pratio*100, 6)}%'
    return s

def convert_attrib_prop_state(prop_code, state):
    if prop_code in inverted_prop_codes(): state = not state
    return state

def build_fpdict(board):
    fpdict = {}
    for fp in board.GetFootprints():
        uuid = fp_to_uuid(fp)
        # if UUID is already present, skip any footprint with same UUID.
        # TODO return error if same UUIDs are found. silently ignoring entries is bad.
        if not uuid in fpdict:
            fields_text = fp.GetFieldsText()
            paste_margin_ratio = fp.GetLocalSolderPasteMarginRatio()
            fpdict[uuid] = {}
            fpdict[uuid][Key.REF] = fp.GetReferenceAsString()
            fpdict[uuid][Key.FIELDS] = {}
            for field in fields_text:
                if field_accepted(field): fpdict[uuid][Key.FIELDS][field] = fields_text[field]
            fpdict[uuid][Key.VALUE] = fp.GetValue()
            fpdict[uuid][Key.PROPS] = {}
            fpdict[uuid][Key.PROPS][PropCode.FIT]    = convert_attrib_prop_state(PropCode.FIT,    fp.IsDNP())
            fpdict[uuid][Key.PROPS][PropCode.BOM]    = convert_attrib_prop_state(PropCode.BOM,    fp.IsExcludedFromBOM())
            fpdict[uuid][Key.PROPS][PropCode.POS]    = convert_attrib_prop_state(PropCode.POS,    fp.IsExcludedFromPosFiles())
            fpdict[uuid][Key.PROPS][PropCode.SOLDER] = paste_state_from_ratio(paste_margin_ratio)
            for i, model in enumerate(fp.Models()): apply_indexed_prop(fpdict[uuid][Key.PROPS], PropCode.MODEL, i + 1, model.m_Show)
            fpdict[uuid][Key.RAW] = {}
            fpdict[uuid][Key.RAW][Key.PRATIO] = paste_margin_ratio
    return fpdict

def store_fpdict(board, fpdict):
    for uuid in fpdict:
        fp = uuid_to_fp(board, uuid)
        old_fp_value = fp.GetValue()
        new_fp_value = fpdict[uuid][Key.VALUE]
        if old_fp_value != new_fp_value:
            fp.SetValue(new_fp_value)
        for prop_id in fpdict[uuid][Key.PROPS]:
            prop_code, prop_index = split_prop_id(prop_id)
            new_prop_value = fpdict[uuid][Key.PROPS][prop_id]
            if new_prop_value is not None:
                old_prop_value = None
                if   prop_code == PropCode.FIT:   old_prop_value = convert_attrib_prop_state(prop_code, fp.IsDNP())
                elif prop_code == PropCode.BOM:   old_prop_value = convert_attrib_prop_state(prop_code, fp.IsExcludedFromBOM())
                elif prop_code == PropCode.POS:   old_prop_value = convert_attrib_prop_state(prop_code, fp.IsExcludedFromPosFiles())
                elif prop_code == PropCode.MODEL: old_prop_value = convert_attrib_prop_state(prop_code, fp.Models()[prop_index - 1].m_Show)
                if old_prop_value is not None:
                    if old_prop_value != new_prop_value:
                        if   prop_code == PropCode.FIT:   fp.SetDNP(convert_attrib_prop_state(prop_code, new_prop_value))
                        elif prop_code == PropCode.BOM:   fp.SetExcludedFromBOM(convert_attrib_prop_state(prop_code, new_prop_value))
                        elif prop_code == PropCode.POS:   fp.SetExcludedFromPosFiles(convert_attrib_prop_state(prop_code, new_prop_value))
                        elif prop_code == PropCode.MODEL: fp.Models()[prop_index - 1].m_Show = convert_attrib_prop_state(prop_code, new_prop_value)
        old_pratio_value = fp.GetLocalSolderPasteMarginRatio()
        new_pratio_value = fpdict[uuid][Key.RAW][Key.PRATIO]
        if old_pratio_value != new_pratio_value:
            fp.SetLocalSolderPasteMarginRatio(new_pratio_value)
        old_fp_field_values = fp.GetFieldsText()
        for field in fpdict[uuid][Key.FIELDS]:
            old_fp_field_value = old_fp_field_values[field]
            new_fp_field_value = fpdict[uuid][Key.FIELDS][field]
            if old_fp_field_value != new_fp_field_value:
                set_fp_field(fp, field, new_fp_field_value)
    return fpdict

def bool_as_text(value):
    return 'true' if value == True else 'false'

def natural_sort_key(string):
    key = []
    part = ''
    for c in string:
        if c.isdigit(): part += c
        else:
            if part:
                key.append((0, int(part), ''))
                part = ''
            key.append((1, 0, c.lower()))
    if part: key.append((0, int(part), ''))
    return key

def escape_str(string):
    result = ''
    for c in string:
        if c == '\\' or c == "'" or c == '"': result += '\\'
        result += c
    return result

def quote_str(string):
    # we prefer single-quotes for output
    if string == '': result = "''"
    else:
        if any(c in string for c in ', -~\\[]()="\''):
            q = '"' if string.count("'") > string.count('"') else "'"
            result = q
            for c in string:
                if c == '\\' or c == q: result += '\\'
                result += c
            result += q
        else: result = string
    return result

class Key:
    DEFAULT = '*' # same symbol as used for expressions
    STANDIN = '?' # same symbol as used for expressions
    ASPECT  = 'a'
    CMP     = 'c'
    FLD     = 'f'
    VALUE   = 'v'
    PROPS   = 'p'
    REF     = 'R'
    FIELDS  = 'F'
    RAW     = 'Raw'
    PRATIO  = 'PR'

class PropCode: # all of these must be uppercase
    FIT    = 'F'
    BOM    = 'B'
    POS    = 'P'
    SOLDER = 'S'
    MODEL  = 'M'

class PropGroup:
    ASSEMBLE = '!'

class FieldID: # case-sensitive
    BASE   = None
    ASPECT = 'Aspect'

def base_prop_codes(): return PropCode.FIT + PropCode.BOM + PropCode.POS + PropCode.SOLDER + PropCode.MODEL

def supported_prop_codes(): return base_prop_codes() + PropGroup.ASSEMBLE

def inverted_prop_codes(): return PropCode.FIT + PropCode.BOM + PropCode.POS

def indexed_prop_codes(): return PropCode.MODEL

def group_assemble_prop_codes(): return PropCode.FIT + PropCode.BOM + PropCode.POS

def prop_state(props, prop):
    return props[prop] if prop in props else None

def prop_attrib_descr(prop_id):
    prop_code, prop_index = split_prop_id(prop_id)
    name = '(unknown)'
    if prop_code is not None:
        if   prop_code == PropCode.BOM:    name = "'Exclude from bill of materials'"
        elif prop_code == PropCode.POS:    name = "'Exclude from position files'"
        elif prop_code == PropCode.FIT:    name = "'Do not populate'"
        elif prop_code == PropCode.SOLDER: name = 'solder paste relative clearance'
        elif prop_code == PropCode.MODEL:  name = 'visibility of 3D model'
        if prop_index is not None:         name += ' #' + str(prop_index)
    return name

def prop_abbrev(prop_id):
    prop_code, prop_index = split_prop_id(prop_id)
    name = '(unknown)'
    if prop_code is not None:
        if   prop_code == PropCode.BOM:    name = 'Bom'
        elif prop_code == PropCode.POS:    name = 'Pos'
        elif prop_code == PropCode.FIT:    name = 'Fit'
        elif prop_code == PropCode.SOLDER: name = 'Solder'
        elif prop_code == PropCode.MODEL:  name = 'Model'
        if prop_index is not None:         name += '#' + str(prop_index)
    return name

def mismatches_fp_choice(fpdict_branch, vardict_choice_branch):
    # TODO in returned mismatches, add mismatching fp and choice states
    mismatches = []
    choice_value = vardict_choice_branch[Key.VALUE]
    fp_value = fpdict_branch[Key.VALUE]
    if choice_value is not None and fp_value != choice_value:
        mismatches.append('value')
    for prop_id in fpdict_branch[Key.PROPS]:
        choice_prop = prop_state(vardict_choice_branch[Key.PROPS], prop_id)
        fp_prop = prop_state(fpdict_branch[Key.PROPS], prop_id)
        if choice_prop is not None and fp_prop is not None and choice_prop != fp_prop:
            mismatches.append(f"prop '{prop_id}'")
    return mismatches

def mismatches_fp_choice_fld(fp_fields, vardict_fld_branch, choice):
    # TODO in returned mismatches, add mismatching fp and choice states
    mismatches = []
    for field in vardict_fld_branch:
        choice_field_value = vardict_fld_branch[field][choice][Key.VALUE]
        if choice_field_value is not None and choice_field_value != fp_fields[field]:
            mismatches.append(f"field '{field}' for choice '{choice}'")
    return mismatches

def detect_current_choices(fpdict, vardict):
    # We start with the usual Choice dict filled with all possible Choices per Aspect and then
    # eliminate all Choices whose values do not exactly match the actual FP values, fields or attributes.
    # If exactly one Choice per Aspect remains, then we add this choice to the selection dict.
    choices = get_choice_dict(vardict)
    # Eliminate Choices not matching the actual FP values.
    for uuid in vardict:
        fp_ref = fpdict[uuid][Key.REF]
        aspect = vardict[uuid][Key.ASPECT]
        eliminate_choices = []
        for choice in choices[aspect]:
            eliminate = False
            mismatches = mismatches_fp_choice(fpdict[uuid], vardict[uuid][Key.CMP][choice])
            if mismatches: eliminate = True
            mismatches = mismatches_fp_choice_fld(fpdict[uuid][Key.FIELDS], vardict[uuid][Key.FLD], choice)
            if mismatches: eliminate = True
            # defer elimination until after iteration
            if eliminate: eliminate_choices.append(choice)
        for choice in eliminate_choices: choices[aspect].remove(choice)
    # Create a dict with candidate Choices. Report Choices only if they are unambiguous.
    selection = {}
    for aspect in choices:
        if len(choices[aspect]) == 1: selection[aspect] = choices[aspect][0]
        else:                         selection[aspect] = None
    return selection

def apply_selection(fpdict, vardict, selection, dry_run = False):
    changes = []
    for uuid in vardict:
        ref = fpdict[uuid][Key.REF]
        aspect = vardict[uuid][Key.ASPECT]
        if not aspect in selection: continue
        selected_choice = selection[aspect]
        if selected_choice is None: continue
        choice_text = f'{quote_str(aspect)}={quote_str(selected_choice)}'
        new_value = vardict[uuid][Key.CMP][selected_choice][Key.VALUE]
        if new_value is not None:
            old_value = fpdict[uuid][Key.VALUE]
            if old_value != new_value:
                changes.append([uuid, ref, f"Change {ref} value from '{escape_str(old_value)}' to '{escape_str(new_value)}' ({choice_text})."])
                if not dry_run: fpdict[uuid][Key.VALUE] = new_value
        for prop_id in fpdict[uuid][Key.PROPS]:
            new_state = vardict[uuid][Key.CMP][selected_choice][Key.PROPS][prop_id]
            if new_state is not None:
                old_state = fpdict[uuid][Key.PROPS][prop_id]
                if old_state is not None and new_state != old_state:
                    old_state_text = None
                    new_state_text = None
                    new_pratio = None
                    if prop_id == PropCode.SOLDER:
                        # calculate new solder paste margin ratio
                        old_pratio = fpdict[uuid][Key.RAW][Key.PRATIO]
                        old_pmode = paste_mode_from_ratio(old_pratio)
                        if new_state: # off -> on
                            if   old_pmode == PasteMode.OFF_WAS_INHERIT: new_pratio = None
                            elif old_pmode == PasteMode.OFF_WAS_RATIO:   new_pratio = old_pratio - PasteRatio.OFFSET
                            else: raise ValueError(f"Unexpected old paste mode ({old_pmode}) in OFF-to-ON transition")
                        else: # on -> off
                            if   old_pmode == PasteMode.ON_IS_INHERIT: new_pratio = PasteRatio.INHERIT
                            elif old_pmode == PasteMode.ON_WITH_RATIO: new_pratio = old_pratio + PasteRatio.OFFSET
                            else: raise ValueError(f"Unexpected old paste mode ({old_pmode}) in ON-to-OFF transition")
                        old_state_text = paste_ratio_text(old_pratio)
                        new_state_text = paste_ratio_text(new_pratio)
                    else:
                        old_state_text = f"'{bool_as_text(convert_attrib_prop_state(prop_id, old_state))}'"
                        new_state_text = f"'{bool_as_text(convert_attrib_prop_state(prop_id, new_state))}'"
                    if not (old_state_text is None or new_state_text is None):
                        changes.append([uuid, ref, f"Change {ref} {prop_attrib_descr(prop_id)} from {old_state_text} to {new_state_text} ({choice_text})."])
                        if not dry_run:
                            fpdict[uuid][Key.PROPS][prop_id] = new_state
                            if prop_id == PropCode.SOLDER: fpdict[uuid][Key.RAW][Key.PRATIO] = new_pratio
        for field in vardict[uuid][Key.FLD]:
            new_field_value = vardict[uuid][Key.FLD][field][selected_choice][Key.VALUE]
            if new_field_value is not None:
                old_field_value = fpdict[uuid][Key.FIELDS][field]
                if old_field_value != new_field_value:
                    changes.append([uuid, ref, f"Change {ref} field '{escape_str(field)}' from '{escape_str(old_field_value)}' to '{escape_str(new_field_value)}' ({choice_text})."])
                    if not dry_run: fpdict[uuid][Key.FIELDS][field] = new_field_value
    return changes

def apply_indexed_prop(prop_set, prop_code, prop_index, state):
    if not (prop_code is None or prop_index is None):
        prop_set[prop_code + '#' + str(prop_index)] = state

def split_prop_id(prop_id):
    prop_code = None
    prop_index = None
    if len(prop_id) > 2 and prop_id[1] == '#' and prop_id[0] in indexed_prop_codes() and prop_id[2:].isnumeric():
        prop_code = prop_id[0]
        prop_index = int(prop_id[2:])
    elif len(prop_id) == 1 and prop_id[0] in supported_prop_codes():
        prop_code = prop_id[0]
    return prop_code, prop_index

def parse_prop_str(prop_str, prop_set):
    state = None
    expect_code = expect_index = False
    current_code = current_index = None
    for c in prop_str.upper():
        if c in '+-':
            if expect_code:  raise ValueError(f"Got property modifier where property identifier was expected")
            if expect_index: raise ValueError(f"Got property modifier where property index was expected")
            apply_indexed_prop(prop_set, current_code, current_index, state)
            current_code = current_index = None
            expect_code = True
            state = c == '+'
        elif c in supported_prop_codes():
            if expect_index: raise ValueError(f"Got property code where property index was expected")
            if state is None: raise ValueError(f"Undefined property modifier for identifier '{c}'") # should not happen if caller identifies prop_str by first character
            apply_indexed_prop(prop_set, current_code, current_index, state)
            expect_code = False
            if c in indexed_prop_codes():
                current_code = c
                current_index = 0
                expect_index = True
            else:
                current_code = current_index = None
                if c == PropGroup.ASSEMBLE:
                    for c in group_assemble_prop_codes(): prop_set[c] = state
                else:
                    prop_set[c] = state
        elif c.isnumeric():
            if current_code is None or current_index is None: raise ValueError(f"Got unexpected property index")
            current_index = current_index * 10 + int(c)
            if current_index > 99999: raise ValueError(f"Index value for property code '{current_code}' is too high")
            expect_index = False
        else:
            if not c in supported_prop_codes(): raise ValueError(f"Unsupported property code '{c}'")
    apply_indexed_prop(prop_set, current_code, current_index, state)
    if expect_code:  raise ValueError(f"End of property specifier when property code was expected")
    if expect_index: raise ValueError(f"End of property specifier when property index was expected")

def add_choice(vardict, uuid, raw_choice_name, raw_choice_def, field=None):
    """ Adds a choice set (component or field rule definition) to the vardict. """
    # TODO add unique error codes (for field scope, add an offset), for unit testing (do not compare error strings).
    # If field is passed, this handles field scope rules, else component scope rules.
    field_scope = field is not None
    try:
        raw_names = split_raw_str(raw_choice_name, ',', False)
    except Exception as e:
        return [f"Choice identifiers splitter error for identifier list '{raw_choice_name}': {str(e)}"] # TODO cook name?
    try:
        raw_args = split_raw_str(raw_choice_def, ' ', True)
    except Exception as e:
        return [f"Choice arguments splitter error for argument list '{raw_choice_def}': {str(e)}"]
    choices = []
    for choice_name in raw_names:
        cooked_name = cook_raw_string(choice_name)
        if cooked_name == '':
            return ["Empty choice identifier"]
        choices.append(cooked_name)
    errors = []
    values = []
    prop_set = {}
    for raw_arg in raw_args:
        arg = cook_raw_string(raw_arg)
        if raw_arg[0] in '-+': # not supposed to match if arg starts with \-, \+, '+' or '-'
            if field_scope:
                errors.append(f"No property specifiers allowed in field-scope records")
                continue
            try:
                parse_prop_str(arg, prop_set)
            except Exception as error:
                errors.append(f"Property specifier parser error: {str(error)}")
                continue
        else:
            values.append(arg)
    for choice in choices:
        if field_scope:
            if not field in vardict[uuid][Key.FLD]: vardict[uuid][Key.FLD][field] = {}
            vardict_branch = vardict[uuid][Key.FLD][field]
        else:
            vardict_branch = vardict[uuid][Key.CMP]
        if not choice in vardict_branch:
            vardict_branch[choice] = {}
            vardict_branch[choice][Key.VALUE] = None
            vardict_branch[choice][Key.PROPS] = {}
        if values:
            value = ' '.join(values)
            if vardict_branch[choice][Key.VALUE] is None:
                vardict_branch[choice][Key.VALUE] = value
            else:
                errors.append(f"Illegal additional content '{value}' assignment for choice '{choice}'")
        for prop_code in prop_set:
            if not prop_code in vardict_branch[choice][Key.PROPS]:
                vardict_branch[choice][Key.PROPS][prop_code] = None
            if vardict_branch[choice][Key.PROPS][prop_code] is None:
                vardict_branch[choice][Key.PROPS][prop_code] = prop_set[prop_code]
            else:
                errors.append(f"Illegal additional '{prop_abbrev(prop_code)}' property assignment for choice '{choice}'")
    return errors

def finalize_vardict_branch(vardict_branch, all_aspect_choices, fp_props=None):
    """
    Finalizes (flattens) a branch of the vardict (either component or field scope).
    To specify a field scope branch, pass None for the fp_props parameter.
    """
    errors = []
    # Flatten values
    # TODO instead of counting, append (quoted) name of choice to two lists,
    #      then print their content (joined with comma) in the error messages!
    # check for mixed defined and undefined content
    choices_with_value_defined = 0
    for choice in all_aspect_choices:
        if not choice in vardict_branch:
            vardict_branch[choice] = {}
            if Key.STANDIN in vardict_branch:
                vardict_branch[choice][Key.VALUE] = vardict_branch[Key.STANDIN][Key.VALUE]
                vardict_branch[choice][Key.PROPS] = vardict_branch[Key.STANDIN][Key.PROPS]
            else:
                vardict_branch[choice][Key.VALUE] = None
                vardict_branch[choice][Key.PROPS] = {}
        if Key.DEFAULT in vardict_branch:
            if vardict_branch[choice][Key.VALUE] is None:
                vardict_branch[choice][Key.VALUE] = vardict_branch[Key.DEFAULT][Key.VALUE]
        if vardict_branch[choice][Key.VALUE] is not None:
            choices_with_value_defined += 1
    if not (choices_with_value_defined == 0 or choices_with_value_defined == len(all_aspect_choices)):
        errors.append(f"Mixed choices with defined ({choices_with_value_defined}x) and undefined ({len(all_aspect_choices) - choices_with_value_defined}x) content (either all or none must be defined)")
    if fp_props is not None:
        # Flatten properties
        for prop_code in fp_props:
            default_prop_value = None
            if Key.DEFAULT in vardict_branch and prop_code in vardict_branch[Key.DEFAULT][Key.PROPS] and vardict_branch[Key.DEFAULT][Key.PROPS][prop_code] is not None:
                # defined default prop value available, use it
                default_prop_value = vardict_branch[Key.DEFAULT][Key.PROPS][prop_code]
            else:
                # try to determine an implicit default value
                choices_with_true = 0
                choices_with_false = 0
                for choice in all_aspect_choices:
                    if prop_code in vardict_branch[choice][Key.PROPS] and vardict_branch[choice][Key.PROPS][prop_code] is not None:
                        if vardict_branch[choice][Key.PROPS][prop_code]: choices_with_true  += 1
                        else:                                            choices_with_false += 1
                # if only set x-or clear is used in (specific) choices, the implicit default is the opposite
                if   choices_with_false and not choices_with_true: default_prop_value = True
                elif not choices_with_false and choices_with_true: default_prop_value = False
            # check for mixed defined and undefined properties
            choices_with_prop_defined = 0
            for choice in all_aspect_choices:
                if not prop_code in vardict_branch[choice][Key.PROPS] or vardict_branch[choice][Key.PROPS][prop_code] is None:
                    vardict_branch[choice][Key.PROPS][prop_code] = default_prop_value
                if vardict_branch[choice][Key.PROPS][prop_code] is not None:
                    choices_with_prop_defined += 1
            if not (choices_with_prop_defined == 0 or choices_with_prop_defined == len(all_aspect_choices)):
                errors.append(f"Mixed choices with defined ({choices_with_prop_defined}x) and undefined ({len(all_aspect_choices) - choices_with_prop_defined}x) {prop_abbrev(prop_code)} property ('{prop_code}') state (either all or none must be defined)")
    # Remove default choice entries from branch
    vardict_branch.pop(Key.DEFAULT, None)
    vardict_branch.pop(Key.STANDIN, None)
    return errors

def parse_rule_str(rule_str):
    errors = []
    aspects = []
    choice_sets = []
    if rule_str is not None:
        try:
            rule_sections = split_raw_str(rule_str, ' ', True)
        except Exception as error:
            return [f'Combined record splitter: {str(error)}'], None, None
        for section in rule_sections:
            try:
                name_list, content = split_parens(section)
            except Exception as error:
                errors.append(f'Choice expression splitter: {str(error)}')
                continue
            if content is None: # None means: no parens
                # this is an aspect name. cook and store.
                try:
                    cooked_name = cook_raw_string(name_list)
                except Exception as error:
                    errors.append(f'Aspect identifier parser: {str(error)}')
                    continue
                if cooked_name is None or cooked_name == '':
                    errors.append('Aspect identifier must not be empty')
                    continue
                aspects.append(cooked_name)
            else:
                # this is a choice definition. leave name and content raw and store.
                if name_list is None or name_list == '':
                    errors.append('Choice identifier list must not be empty')
                    continue
                choice_sets.append([name_list, content])
    return errors, aspects, choice_sets

def field_name_check(field_name, available_fields):
    error = None
    if not field_accepted(field_name):
        error = f"Target field '{field_name}' is forbidden" # TODO escape field name
    elif not field_name in available_fields:
        error = f"Target field '{field_name}' does not exist" # TODO escape field name
    return error

def determine_fieldID_base(fpdict):
    global FieldIDOptions
    # looking for any component which has a field with one of the 
    # available field options as the base
    # first non-empty one found wins
    for base in FieldIDOptions:
        for uuid in fpdict:
            fpdict_uuid_branch = fpdict[uuid]
            for fp_field in fpdict_uuid_branch[Key.FIELDS]:
                value = fpdict_uuid_branch[Key.FIELDS][fp_field]
                if value is None or not len(value):
                    # the field is here but its just empty, ignore
                    continue
                try: parts = split_raw_str(fp_field, '.', False)
                except: continue
                if len(parts) == 2 and parts[0] == base and parts[1] == FieldID.ASPECT:
                    return base
                elif len(parts) == 1 and parts[0] == base:
                    return base
                elif len(parts) > 1 and parts[-1] == base:
                    return base
                else:
                    try: prefix, name_list = split_parens(parts[-1])
                    except: continue
                    if prefix == base:
                        return base
    
    return None
         
    

def parse_rule_fields(fpdict_uuid_branch):
    errors = []
    aspect = None
    cmp_rule_string = None
    cmp_choice_sets = []
    fld_rule_strings = []
    fld_choice_sets = []
    for fp_field in fpdict_uuid_branch[Key.FIELDS]:
        value = fpdict_uuid_branch[Key.FIELDS][fp_field]
        # field names that are not properly formatted are ignored and do not cause
        # an error message. we don't want to misinterpret user's fields.
        try: parts = split_raw_str(fp_field, '.', False)
        except: continue
        if len(parts) == 2 and parts[0] == FieldID.BASE and parts[1] == FieldID.ASPECT:
            aspect = value
        elif len(parts) == 1 and parts[0] == FieldID.BASE:
            cmp_rule_string = value
        elif len(parts) > 1 and parts[-1] == FieldID.BASE:
            target_field = '.'.join(parts[0:-1])
            field_name_error = field_name_check(target_field, fpdict_uuid_branch[Key.FIELDS])
            if field_name_error is not None:
                errors.append(f"Combined field record: {field_name_error}")
                continue
            else:
                fld_rule_strings.append([target_field, value])
        else:
            try: prefix, name_list = split_parens(parts[-1])
            except: continue
            if prefix == FieldID.BASE:
                try: parts_in_parens = split_raw_str(name_list, ' ', True)
                except: continue
                if len(parts_in_parens) > 1:
                    errors.append(f"Choice identifier list '{name_list}' contains illegal space character")
                    continue
                if len(parts) == 1:
                    cmp_choice_sets.append([name_list, value])
                else:
                    target_field = '.'.join(parts[0:-1])
                    field_name_error = field_name_check(target_field, fpdict_uuid_branch[Key.FIELDS])
                    if field_name_error is not None:
                        errors.append(f"Simple field record: {field_name_error}")
                        continue
                    else:
                        fld_choice_sets.append([target_field, name_list, value])
    return errors, aspect, cmp_rule_string, cmp_choice_sets, fld_rule_strings, fld_choice_sets

def build_vardict(fpdict):
    vardict = {}
    errors = []
    fld_dict = {}
    all_choices = {}
    # Handle component rule
    if FieldID.BASE is None:
        # if we've not decided on the BASE yet,
        # scan through
        f_base = determine_fieldID_base(fpdict)
        if f_base is None:
            # nothing found
            # set a default, just to satisfy any 
            # other parts of the system/error messages, etc
            FieldID.BASE = FieldIDOptions[0]
            return vardict, errors
        # found something, yay. Remember that.
        FieldID.BASE = f_base

    for uuid in fpdict:
        ref = fpdict[uuid][Key.REF]
        parse_errors, aspect, cmp_rule_string, cmp_choice_sets, fld_rule_strings, fld_choice_sets = parse_rule_fields(fpdict[uuid])
        if parse_errors:
            for parse_error in parse_errors: errors.append([uuid, ref, f"{ref}: Field parser: {parse_error}."])
            continue
        parse_errors, aspects, choice_sets = parse_rule_str(cmp_rule_string)
        if parse_errors:
            for parse_error in parse_errors: errors.append([uuid, ref, f"{ref}: Component-scope record parser: {parse_error}."])
            continue
        choice_sets.extend(cmp_choice_sets)
        # TODO decide: shall we really use uncooked aspect name? we have the whole field content only for the pure value.
        if len(aspects) > 1:
            errors.append([uuid, ref, f"{ref}: Found multiple aspect identifiers."])
            continue
        elif len(aspects) == 1:
            # about to use the aspect name from the component rule ...
            if aspect is not None and aspect != '':
                # ... but there is already an aspect set via the aspect field
                errors.append([uuid, ref, f"{ref}: Conflicting aspect identifier specification styles (combined component-scope record vs. aspect field)."])
                continue
            aspect = aspects[0]
        if aspect is None or aspect == '':
            if choice_sets:
                errors.append([uuid, ref, f"{ref}: Component record(s) found, but missing an aspect identifier."])
            continue
        if uuid in vardict:
            errors.append([uuid, ref, f"{ref}: Found multiple footprints with same UUID containing component-scope records."])
            continue
        vardict[uuid] = {}
        vardict[uuid][Key.ASPECT] = aspect
        vardict[uuid][Key.CMP] = {}
        vardict[uuid][Key.FLD] = {}
        fld_dict[uuid] = [fld_rule_strings, fld_choice_sets] # save for fld loop
        for choice_name, choice_content in choice_sets:
            add_errors = add_choice(vardict, uuid, choice_name, choice_content)
            if add_errors:
                for error in add_errors: errors.append([uuid, ref, f"{ref}: When adding aspect '{aspect}' choice list '{choice_name}' in component record: {error}."])
                break
    # Handle field scope
    for uuid in fld_dict:
        ref = fpdict[uuid][Key.REF]
        aspect = vardict[uuid][Key.ASPECT]
        fld_rule_strings, fld_choice_sets = fld_dict[uuid]
        if fld_rule_strings and aspect is None:
            errors.append([uuid, ref, f"{ref}: Combined field record(s) found, but missing an aspect identifier."])
            continue
        valid = False
        for field, rule_str in fld_rule_strings:
            if rule_str is None or rule_str == '': continue
            parse_errors, aspects, choice_sets = parse_rule_str(rule_str)
            if parse_errors:
                for parse_error in parse_errors: errors.append([uuid, ref, f"{ref}: Combined field record parser for target field '{field}': {parse_error}."])
                continue
            if aspects:
                errors.append([uuid, ref, f"{ref}: Combined field record for target field '{field}' contains what looks like an aspect identifier (only allowed in combined component-scope records)."])
                continue
            if field in vardict[uuid][Key.FLD]:
                errors.append([uuid, ref, f"{ref}: Multiple assignments for target field '{field}'."])
                continue
            for choice_name, choice_content in choice_sets:
                add_errors = add_choice(vardict, uuid, choice_name, choice_content, field)
                if add_errors:
                    for error in add_errors: errors.append([uuid, ref, f"{ref}: Combined field record for aspect '{aspect}' choice list '{choice_name}' with target field '{field}': {error}."])
                    break
        else:
            valid = True
        if not valid: continue
        if fld_choice_sets and aspect is None:
            errors.append([uuid, ref, f"{ref}: Simple field record(s) found, but missing an aspect identifier."])
            continue
        valid = False
        for field, choice_name, choice_content in fld_choice_sets:
            add_errors = add_choice(vardict, uuid, choice_name, choice_content, field)
            if add_errors:
                for error in add_errors: errors.append([uuid, ref, f"{ref}: Simple field record for aspect '{aspect}' choice list '{choice_name}' with target field '{field}': {error}."])
                break
        else:
            valid = True
        if not valid: continue
    all_choices = get_choice_dict(vardict)
    for aspect in all_choices:
        if len(all_choices[aspect]) == 0:
            errors.append([None, '0', f"Aspect '{escape_str(aspect)}' has no choices defined."])
    for uuid in fld_dict:
        ref = fpdict[uuid][Key.REF]
        aspect = vardict[uuid][Key.ASPECT]
        fin_errors = finalize_vardict_branch(vardict[uuid][Key.CMP], all_choices[aspect], fpdict[uuid][Key.PROPS])
        if fin_errors:
            # TODO cook and quote names in error message, refine wording
            for e in fin_errors: errors.append([uuid, ref, f"{ref}: In component record: {e}."])
            continue
        for field in vardict[uuid][Key.FLD]:
            fin_errors = finalize_vardict_branch(vardict[uuid][Key.FLD][field], all_choices[aspect])
            if fin_errors:
                # TODO cook and quote names in error message, refine wording
                for e in fin_errors: errors.append([uuid, ref, f"{ref}: In field record for target field '{field}': {e}."])
                continue
    # Check that all solder paste margin values match one of the two allowed ranges (only if the corresponding property is used, else the current value is ignored)
    for uuid in vardict:
        for choice in vardict[uuid][Key.CMP]:
            if (PropCode.SOLDER in vardict[uuid][Key.CMP][choice][Key.PROPS]) and (not vardict[uuid][Key.CMP][choice][Key.PROPS][PropCode.SOLDER] is None) and (Key.PRATIO in fpdict[uuid][Key.RAW]):
                pratio = fpdict[uuid][Key.RAW][Key.PRATIO]
                if paste_state_from_ratio(pratio) is None:
                    ref = fpdict[uuid][Key.REF]
                    errors.append([uuid, ref, f"{ref}: Cannot classify current solder paste relative clearance ({paste_ratio_text(pratio)}) to be used for '{prop_abbrev(PropCode.SOLDER)}' property."])
                    break
    # Check that all (indexed) properties are really present in the fpdict (extra loops for dedicated break handling)
    for uuid in vardict:
        for choice in vardict[uuid][Key.CMP]:
            for prop_id in vardict[uuid][Key.CMP][choice][Key.PROPS]:
                if not prop_id in fpdict[uuid][Key.PROPS]:
                    prop_code, prop_index = split_prop_id(prop_id)
                    ref = fpdict[uuid][Key.REF]
                    errors.append([uuid, ref, f"{ref}: Cannot match property '{prop_abbrev(prop_id)}' to footprint (probably index out of bounds)."])
    if not errors:
        # Check for ambiguous choices (only if data is valid so far)
        check_dict = {} # structure: /aspect/choice/uuid/...
        for uuid in vardict:
            aspect = vardict[uuid][Key.ASPECT]
            if not aspect in check_dict: check_dict[aspect] = {}
            for choice in all_choices[aspect]:
                if not choice in check_dict[aspect]: check_dict[aspect][choice] = {}
                if not uuid in check_dict[aspect][choice]: check_dict[aspect][choice][uuid] = {}
                # copy reference to component-scope sub-tree
                check_dict[aspect][choice][uuid][Key.CMP] = vardict[uuid][Key.CMP][choice]
                # copy references to field-scope sub-trees (per field)
                if not Key.FLD in check_dict[aspect][choice][uuid]: check_dict[aspect][choice][uuid][Key.FLD] = {}
                for field in vardict[uuid][Key.FLD]:
                    if not field in check_dict[aspect][choice][uuid][Key.FLD]: check_dict[aspect][choice][uuid][Key.FLD][field] = {}
                    check_dict[aspect][choice][uuid][Key.FLD][field] = vardict[uuid][Key.FLD][field][choice]
        for aspect in sorted(all_choices, key=natural_sort_key):
            choices = sorted(all_choices[aspect], key=natural_sort_key)
            reported = []
            for choice_a in choices: # matrix rows
                if choice_a in reported: continue
                ambiguous = []
                for choice_b in reversed(choices): # matrix columns
                    if choice_a == choice_b: break # at main diagonal, only check upper triangle
                    if not choice_b in reported:
                        if check_dict[aspect][choice_a] == check_dict[aspect][choice_b]:
                            ambiguous.append(choice_b)
                if ambiguous:
                    ambiguous.append(choice_a)
                    choice_names = map(lambda x: f"'{escape_str(x)}'", reversed(ambiguous))
                    errors.append([None, '0', f"Illegal ambiguity: Aspect '{escape_str(aspect)}' has equivalent choices {', '.join(choice_names)}."])
                    reported.extend(ambiguous)
    if errors: vardict = None # make sure an incomplete vardict cannot be used by the caller
    return vardict, errors

def get_choice_dict(vardict):
    choices = {}
    for uuid in vardict:
        aspect = vardict[uuid][Key.ASPECT]
        if not aspect in choices: choices[aspect] = []
        # In case the input dict still contains temporary data (such as default data), ignore it.
        for choice in vardict[uuid][Key.CMP]:
            if choice != Key.DEFAULT and choice != Key.STANDIN and not choice in choices[aspect]: choices[aspect].append(choice)
        for field in vardict[uuid][Key.FLD]:
            for choice in vardict[uuid][Key.FLD][field]:
                if choice != Key.DEFAULT and choice != Key.STANDIN and not choice in choices[aspect]: choices[aspect].append(choice)
    return choices

def split_parens(string):
    item = []
    outside = None
    inside = None
    escaped = False
    quoted_s = False
    quoted_d = False
    parens = 0
    end_expected = False
    for c in string:
        if end_expected: raise ValueError('String extends beyond closing parenthesis')
        elif escaped:
            escaped = False
            item.append(c)
        elif c == '\\':
            escaped = True
            item.append(c)
        elif c == "'" and not quoted_d:
            quoted_s = not quoted_s
            item.append(c)
        elif c == '"' and not quoted_s:
            quoted_d = not quoted_d
            item.append(c)
        elif c == '(' and not (quoted_s or quoted_d):
            parens += 1
            if parens == 1:
                outside = ''.join(item)
                inside = '' # inside: no parens -> None, empty parens -> ''
                item = []
            else:
                item.append(c)
        elif c == ')' and not (quoted_s or quoted_d):
            if parens > 0:
                parens -= 1
                if parens == 0:
                    inside = ''.join(item)
                    item = []
                    end_expected = True
                else:
                    item.append(c)
            else:  raise ValueError('Unmatched closing parenthesis')
        else:
            item.append(c)
    if parens:   raise ValueError('Unmatched opening parenthesis')
    if escaped:  raise ValueError('Unterminated escape sequence (\\) at end of string')
    if quoted_s: raise ValueError("Unmatched single-quote (') character in string")
    if quoted_d: raise ValueError('Unmatched double-quote (") character in string')
    if item: outside = ''.join(item)
    return outside, inside

def split_raw_str(str, sep, multisep):
    result = []
    item = []
    escaped = False
    quoted_s = False
    quoted_d = False
    parens = 0
    for c in str:
        if escaped:
            escaped = False
            item.append(c)
        elif c == '\\':
            escaped = True
            item.append(c)
        elif c == "'" and not quoted_d:
            quoted_s = not quoted_s
            item.append(c)
        elif c == '"' and not quoted_s:
            quoted_d = not quoted_d
            item.append(c)
        elif c == '(' and not (quoted_s or quoted_d):
            parens += 1
            item.append(c)
        elif c == ')' and not (quoted_s or quoted_d):
            if parens > 0:
                parens -= 1
            else: raise ValueError('Unmatched closing parenthesis')
            item.append(c)
        elif c == sep and not (quoted_s or quoted_d) and parens == 0:
            if not multisep or item:
                result.append(''.join(item))
                item = []
        else:
            item.append(c)
    if parens:   raise ValueError('Unmatched opening parenthesis')
    if escaped:  raise ValueError('Unterminated escape sequence (\\) at end of string')
    if quoted_s: raise ValueError("Unmatched single-quote (') character in string")
    if quoted_d: raise ValueError('Unmatched double-quote (") character in string')
    if not multisep or item: result.append(''.join(item))
    return result

def cook_raw_string(string):
    result = []
    escaped  = False
    quoted_s = False
    quoted_d = False
    for c in string:
        if escaped:
            result.append(c)
            escaped = False
        elif c == '\\':
            escaped = True
        elif c == "'" and not quoted_d:
            quoted_s = not quoted_s
        elif c == '"' and not quoted_s:
            quoted_d = not quoted_d
        else:
            result.append(c)
    if escaped:  raise ValueError('Unterminated escape sequence (\\) at end of string')
    if quoted_s: raise ValueError("Unmatched single-quote (') character in string")
    if quoted_d: raise ValueError('Unmatched double-quote (") character in string')
    return ''.join(result)

def count_duplicates(item_list):
    seen = {}
    dups = []
    for item in item_list:
        if item in seen: dups.append(item)
        else: seen[item] = True
    return dups

def count_empty(item_list):
    return sum(1 for item in item_list if item == '')

def did_you_mean(user_input, valid_options):
    suggestions = difflib.get_close_matches(user_input, valid_options, n=1, cutoff=0.6)
    return f' Did you mean "{suggestions[0]}"?' if suggestions else ''

class VariantInfo:
    def __init__(self, pcb_filename):
        ext='.kivar_vdt.csv'
        self._aspects = []
        self._variants = []
        self._choices = {}
        self._hash_at_load = None
        self._is_loaded = False
        self._file_path = os.path.splitext(pcb_filename)[0] + ext if pcb_filename is not None and pcb_filename != '' else None

    def read_csv(self, choice_dict):
        if self._file_path is None or not os.path.exists(self._file_path) or not os.access(self._file_path, os.R_OK):
            return []

        with open(self._file_path, newline='', encoding='utf-8') as csvfile:
            table = list(csv.reader(csvfile))

        # Validate table structure
        errors = []
        if len(table) < 2: # Note: want to allow aspect binding without variant definitions? then change this part!
            errors.append(f'Table has less than two rows.')
        else:
            len_prev = None
            for n, row in enumerate(table):
                len_this = len(row)
                if len_this < 2: # variants without aspect bindings don't make sense
                    errors.append(f'Row {n+1} has less than two columns.')
                    break
                if len_prev is None:
                    len_prev = len_this
                else:
                    if len_this != len_prev:
                        errors.append(f'Row {n+1} has a different number of columns ({len_this}) than previous rows ({len_prev}).')
                        break

        if errors: return errors

        # Load data
        variants = [row[0] for row in table[1:]]
        aspects = table[0][1:]
        choices = {row[0]: row[1:] for row in table[1:]}

        # Validate variant identifiers
        empty = count_empty(variants)
        if empty:
            errors.append(f'Found {empty} empty variant identifiers.')
        dups = count_duplicates(variants)
        if dups:
            dup_report = ', '.join([f'"{dup}"' for dup in dups])
            errors.append(f'Found duplicate variant identifiers: {dup_report}.')

        if errors: return errors

        # Validate aspect references
        empty = count_empty(aspects)
        if empty:
            errors.append(f'Found {empty} empty aspect identifiers.')
        dups = count_duplicates(aspects)
        if dups:
            dup_report = ', '.join([f'"{dup}"' for dup in dups])
            errors.append(f'Found duplicate aspect identifiers: {dup_report}.')
        invs = []
        for aspect in aspects:
            if aspect not in choice_dict:
                errors.append(f'Aspect "{aspect}" is invalid.{did_you_mean(aspect, choice_dict)}')

        if errors: return errors

        # Validate choice references
        for variant in variants:
            for index, aspect in enumerate(aspects):
                choice = choices[variant][index]
                if choice not in choice_dict[aspect]:
                    errors.append(f'For aspect "{aspect}", choice "{choice}" is invalid.{did_you_mean(choice, choice_dict[aspect])}')

        if errors: return errors

        # Validate variant assignments
        seen = {}
        dup = False
        for variant, choice_list in choices.items():
            choice_tuple = tuple(choice_list) # hashable
            if choice_tuple in seen:
                dup = True
                break
            else:
                seen[choice_tuple] = True
        if dup:
            errors.append('Found identical choice assignments for multiple variants.')

        if errors: return errors

        # Finally, use loaded data
        self._hash_at_load = self.current_file_hash()
        self._variants = variants
        self._aspects = aspects
        self._choices = choices
        self._is_loaded = True
        return []

    def write_csv(self, force=False):
        if len(self._variants) == 0 and len(self._aspects) == 0:
            # if table is empty, remove the file
            os.remove(self._file_path)
        else:
            with open(self._file_path, mode='w', newline='', encoding='utf-8') as csvfile:
                csv_writer = csv.writer(csvfile, quoting=csv.QUOTE_ALL)
                csv_writer.writerow([""] + self._aspects)
                for variant in self._variants:
                    csv_writer.writerow([variant] + self._choices[variant])

    def create_table(self, variant, aspects, choice_dict):
        choices = []
        for aspect in aspects:
            choices.append(choice_dict[aspect])
        self._choices = { variant: choices }
        self._variants = [variant]
        self._aspects = aspects
        return True

    def delete_table(self):
        self._aspects = []
        self._variants = []
        self._choices = {}
        return True

    def add_variant(self, variant, choice_dict):
        if variant in self._variants: # double-check, should be blocked
            return False
        else:
            choices = []
            for aspect in self._aspects:
                choices.append(choice_dict[aspect])
            self._choices[variant] = choices
            self._variants.append(variant)
            return True

    def delete_variant(self, variant):
        if variant not in self._variants: # double-check, should be blocked
            return False
        else:
            self._choices.pop(variant)
            self._variants.remove(variant)
            if len(self._variants) == 0:
                self.delete_table()
            return True

    def variants(self):
        return self._variants

    def aspects(self):
        return self._aspects

    def choices(self):
        return self._choices

    def is_loaded(self):
        return self._is_loaded

    def file_path(self):
        return self._file_path

    def current_file_hash(self):
        if os.path.exists(self._file_path) and os.access(self._file_path, os.R_OK):
            algo = hashlib.sha256()
            with open(self._file_path, "rb") as f:
                for chunk in iter(lambda: f.read(4096), b""):
                    algo.update(chunk)
            return algo.hexdigest()
        else:
            return None

    def file_has_changed(self):
        old_hash = self._hash_at_load
        new_hash = self.current_file_hash()
        return new_hash != old_hash

    def match_variant(self, selections):
        matching = []
        for variant in self._variants:
            miss = False
            for index, aspect in enumerate(self._aspects):
                if self._choices[variant][index] != selections[aspect]:
                    miss = True
                    break
            if not miss: matching.append(variant)
        return None if len(matching) != 1 else matching[0]

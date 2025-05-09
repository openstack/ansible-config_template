# (c) 2015, Kevin Carter <kevin.carter@rackspace.com>
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

import base64
import configparser
import datetime
import json
import os
import pwd
import re
import time
import yaml
import tempfile as tmpfilelib

from collections import OrderedDict
from io import StringIO

from ansible.plugins.action import ActionBase
from ansible.module_utils._text import to_bytes, to_text
from ansible.module_utils.parsing.convert_bool import boolean
from ansible import constants as C
from ansible import errors
from ansible.parsing.yaml.dumper import AnsibleDumper
from ansible import __version__ as __ansible_version__

__metaclass__ = type

CONFIG_TYPES = {
    'ini': 'return_config_overrides_ini',
    'json': 'return_config_overrides_json',
    'yaml': 'return_config_overrides_yaml'
}

STRIP_MARKER = '__MARKER__'

if yaml.SafeDumper not in AnsibleDumper.__bases__:
    AnsibleDumper.__bases__ = (yaml.SafeDumper,) + AnsibleDumper.__bases__


class IDumper(AnsibleDumper):
    def increase_indent(self, flow=False, indentless=False):
        return super(IDumper, self).increase_indent(flow, False)


class MultiKeyDict(OrderedDict):
    """Dictionary class which supports duplicate keys.
    This class allows for an item to be added into a standard python dictionary
    however if a key is created more than once the dictionary will convert the
    singular value to a python tuple. This tuple type forces all values to be a
    string.
    Example Usage:
    >>> z = MultiKeyDict()
    >>> z['a'] = 1
    >>> z['b'] = ['a', 'b', 'c']
    >>> z['c'] = {'a': 1}
    >>> print(z)
    ... {'a': 1, 'b': ['a', 'b', 'c'], 'c': {'a': 1}}
    >>> z['a'] = 2
    >>> print(z)
    ... {'a': tuple(['1', '2']), 'c': {'a': 1}, 'b': ['a', 'b', 'c']}
    """

    def index(self, key):
        index_search = [
            i for i, item in enumerate(self) if item.startswith(key)
        ]
        if len(index_search) > 1:
            raise SystemError('Index search returned more than one value')
        return index_search[0]

    def insert(self, index, key, value):
        list(self)[index]  # Validates the index
        shadow = MultiKeyDict()
        counter = 0
        for k, v in self.items():
            if counter == index:
                shadow[k] = v
                shadow[key] = value
            else:
                shadow[k] = v
            counter += 1
        else:
            return shadow

    def update(self, E=None, **kwargs):
        for key, value in E.items():
            super(MultiKeyDict, self).__setitem__(key, value)

    def __setitem__(self, key, value):
        if key in self:
            if isinstance(self[key], tuple):
                items = self[key]
                if str(value) not in items:
                    items += tuple([str(value)])
                    super(MultiKeyDict, self).__setitem__(key, items)
            elif isinstance(self[key], MultiKeyDict):
                pass
            else:
                if str(self[key]) != str(value):
                    items = tuple([str(self[key]), str(value)])
                    super(MultiKeyDict, self).__setitem__(key, items)
        else:
            return super(MultiKeyDict, self).__setitem__(key, value)


class ConfigTemplateParser(configparser.RawConfigParser):
    """configparser which supports multi key value.
    The parser will use keys with multiple variables in a set as a multiple
    key value within a configuration file.
    Default Configuration file:
    [DEFAULT]
    things =
        url1
        url2
        url3
    other = 1,2,3
    [section1]
    key = var1
    key = var2
    key = var3
    Example Usage:
    >>> cp = ConfigTemplateParser(dict_type=MultiKeyDict)
    >>> cp.read('/tmp/test.ini')
    ... ['/tmp/test.ini']
    >>> cp.get('DEFAULT', 'things')
    ... \nurl1\nurl2\nurl3
    >>> cp.get('DEFAULT', 'other')
    ... '1,2,3'
    >>> cp.set('DEFAULT', 'key1', 'var1')
    >>> cp.get('DEFAULT', 'key1')
    ... 'var1'
    >>> cp.get('section1', 'key')
    ... {'var1', 'var2', 'var3'}
    >>> cp.set('section1', 'key', 'var4')
    >>> cp.get('section1', 'key')
    ... {'var1', 'var2', 'var3', 'var4'}
    >>> with open('/tmp/test2.ini', 'w') as f:
    ...     cp.write(f)
    Output file:
    [DEFAULT]
    things =
        url1
        url2
        url3
    key1 = var1
    other = 1,2,3
    [section1]
    key = var4
    key = var1
    key = var3
    key = var2
    """

    def __init__(self, *args, **kwargs):
        self.ignore_none_type = bool(kwargs.pop('ignore_none_type', True))
        self.default_section = str(kwargs.pop('default_section', 'DEFAULT'))
        self.yml_multilines = bool(kwargs.pop('yml_multilines', False))
        self._comment_prefixes = kwargs.pop('comment_prefixes', '/')
        self._empty_lines_in_values = kwargs.get('allow_no_value', True)
        self._strict = kwargs.get('strict', False)
        self._allow_no_value = self._empty_lines_in_values
        configparser.RawConfigParser.__init__(self, *args, **kwargs)

    def set(self, section, option, value=None):
        if not section or section == 'DEFAULT':
            sectdict = self._defaults
            use_defaults = True
        else:
            try:
                sectdict = self._sections[section]
            except KeyError:
                raise SystemError('Section %s not found' % section)
            else:
                use_defaults = False

        option = self.optionxform(option)
        if use_defaults:
            try:
                index = sectdict.index('#%s' % option)
            except (ValueError, IndexError):
                sectdict[option] = value
            else:
                self._defaults = sectdict.insert(index, option, value)
        else:
            sectdict[option] = value

    def _write(self, fp, section, key, item, entry):
        if section:
            # If we are not ignoring a none type value, then print out
            # the option name only if the value type is None.
            if not self.ignore_none_type and item is None:
                fp.write(key + '\n')
                return

        fp.write(entry)

    def _write_check(self, fp, key, value, section=False):
        def _return_entry(option, item):
            # If we have item, we consider it as a config parameter with value
            if item is not None:
                return "%s = %s\n" % (option, str(item).replace('\n', '\n\t'))
            elif not option:
                return option
            else:
                return "%s\n" % option

        key = key.split(STRIP_MARKER)[0]
        if isinstance(value, (tuple, set)):
            for i in sorted(value):
                entry = _return_entry(option=key, item=i)
                self._write(fp, section, key, i, entry)
        elif isinstance(value, list):
            _value = [str(i.replace('\n', '\n\t')) for i in value]
            entry = '%s = %s\n' % (key, ','.join(_value))
            self._write(fp, section, key, value, entry)
        else:
            entry = _return_entry(option=key, item=value)
            self._write(fp, section, key, value, entry)

    def write(self, fp, **kwargs):
        def _do_write(section_name, section, section_bool=False):
            fp.write("[%s]\n" % section_name)
            for key, value in section.items():
                self._write_check(
                    fp,
                    key=key,
                    value=value,
                    section=section_bool
                )

            fp.write("\n")

        if self.default_section != 'DEFAULT':
            if not self._sections.get(self.default_section, False):
                _do_write(
                    section_name=self.default_section,
                    section=self._sections[self.default_section],
                    section_bool=True
                )
        elif self._defaults:
            _do_write('DEFAULT', self._defaults)

        for i in self._sections:
            _do_write(i, self._sections[i], section_bool=True)

    def _read(self, fp, fpname):
        def _temp_set():
            _temp_item = [cursect[optname]]
            cursect.update({optname: _temp_item})

        optname = None
        cursect = {}
        marker_counter = 0
        for lineno, line in enumerate(fp, start=0):
            marker_counter += 1
            mo_match = self.SECTCRE.match(line)
            mo_optcre = self._optcre.match(line)
            if mo_match:
                sectname = mo_match.group('header')
                if sectname in self._sections:
                    cursect = self._sections[sectname]
                elif sectname == 'DEFAULT':
                    cursect = self._defaults
                else:
                    cursect = self._dict()
                    self._sections[sectname] = cursect
            elif mo_optcre:
                optname, vi, optval = mo_optcre.group('option', 'vi', 'value')
                optname = self.optionxform(optname.rstrip())
                if optname and not optname.startswith('#') and optval:
                    if vi in ('=', ':') and ';' in optval:
                        pos = optval.find(';')
                        if pos != -1 and optval[pos - 1].isspace():
                            optval = optval[:pos]
                    optval = optval.strip()
                    if optval == '""':
                        optval = ''
                else:
                    optname = '%s%s-%d' % (
                        optname,
                        STRIP_MARKER,
                        marker_counter
                    )
                cursect[optname] = optval
            else:
                optname = '%s-%d' % (
                    STRIP_MARKER,
                    marker_counter
                )
                cursect[optname] = None


class DictCompare(object):
    """
    Calculate the difference between two dictionaries.

    Example Usage:
    >>> base_dict = {'test1': 'val1', 'test2': 'val2', 'test3': 'val3'}
    >>> new_dict = {'test1': 'val2', 'test3': 'val3', 'test4': 'val3'}
    >>> dc = DictCompare(base_dict, new_dict)
    >>> dc.added()
    ... ['test4']
    >>> dc.removed()
    ... ['test2']
    >>> dc.changed()
    ... ['test1']
    >>> dc.get_changes()
    ... {'added':
    ...     {'test4': 'val3'},
    ...  'removed':
    ...     {'test2': 'val2'},
    ...  'changed':
    ...     {'test1': {'current_val': 'vol1', 'new_val': 'val2'}
    ... }
    """

    def __init__(self, base_dict, new_dict):
        self.new_dict, self.base_dict = new_dict, base_dict
        self.base_items, self.new_items = set(
            self.base_dict.keys()), set(self.new_dict.keys())
        self.intersect = self.new_items.intersection(self.base_items)

    def added(self):
        return self.new_items - self.intersect

    def removed(self):
        return self.base_items - self.intersect

    def changed(self):
        return set(
            x for x in self.intersect if self.base_dict[x] != self.new_dict[x])

    def get_changes(self):
        """Returns dict of differences between 2 dicts and bool indicating if
        there are differences

        :param base_dict: ``dict``
        :param new_dict: ``dict``
        :returns: ``dict``, ``bool``
        """
        changed = False
        mods = {'added': {}, 'removed': {}, 'changed': {}}

        for s in self.changed():
            changed = True
            if type(self.base_dict[s]) is not dict:
                mods['changed'] = {
                    s: {'current_val': self.base_dict[s],
                        'new_val': self.new_dict[s]}}
                continue

            diff = DictCompare(self.base_dict[s], self.new_dict[s])
            for a in diff.added():
                if s not in mods['added']:
                    mods['added'][s] = {a: self.new_dict[s][a]}
                else:
                    mods['added'][s][a] = self.new_dict[s][a]

            for r in diff.removed():
                if s not in mods['removed']:
                    mods['removed'][s] = {r: self.base_dict[s][r]}
                else:
                    mods['removed'][s][r] = self.base_dict[s][r]

            for c in diff.changed():
                if s not in mods['changed']:
                    mods['changed'][s] = {
                        c: {'current_val': self.base_dict[s][c],
                            'new_val': self.new_dict[s][c]}}
                else:
                    mods['changed'][s][c] = {
                        'current_val': self.base_dict[s][c],
                        'new_val': self.new_dict[s][c]}

        for s in self.added():
            changed = True
            mods['added'][s] = self.new_dict[s]

        for s in self.removed():
            changed = True
            mods['removed'][s] = self.base_dict[s]

        return mods, changed


class ActionModule(ActionBase):
    TRANSFERS_FILES = True

    def return_config_overrides_ini(self,
                                    config_overrides,
                                    resultant,
                                    list_extend=True,
                                    ignore_none_type=True,
                                    default_section='DEFAULT',
                                    yml_multilines=False):
        """Returns string value from a modified config file and dict of
        merged config

        :param config_overrides: ``dict``
        :param resultant: ``str`` || ``unicode``
        :returns: ``str``, ``dict``
        """
        def _add_section(section_name):
            # Attempt to add a section to the config file passing if
            #  an error is raised that is related to the section
            #  already existing.
            try:
                config.add_section(section_name)
            except (configparser.DuplicateSectionError, ValueError):
                pass

        config = ConfigTemplateParser(
            allow_no_value=True,
            dict_type=MultiKeyDict,
            ignore_none_type=ignore_none_type,
            default_section=default_section,
            yml_multilines=yml_multilines,
            comment_prefixes='/'
        )
        config.optionxform = str

        config_object = StringIO(resultant)
        config.read_file(config_object)

        if default_section != 'DEFAULT':
            _add_section(section_name=default_section)

        for section, items in config_overrides.items():
            # If the items value is not a dictionary it is assumed that the
            #  value is a default item for this config type.
            if not isinstance(items, dict):
                if isinstance(items, list):
                    items = ','.join(to_text(i) for i in items)

                self._option_write(
                    config,
                    default_section,
                    section,
                    items
                )
            else:
                _add_section(section_name=section)
                for key, value in items.items():
                    try:
                        self._option_write(config, section, key, value)
                    except configparser.NoSectionError as exp:
                        error_msg = str(exp)
                        error_msg += (
                            ' Try being more explicit with your override'
                            'data. Sections are case sensitive.'
                        )
                        raise errors.AnsibleModuleError(error_msg)

        config_object.close()

        config_dict_new = OrderedDict()
        config_defaults = config.defaults()
        for s in config.sections():
            config_dict_new[s] = OrderedDict()
            for k, v in config.items(s):
                if k not in config_defaults or config_defaults[k] != v:
                    config_dict_new[s][k] = v
                else:
                    if default_section in config_dict_new:
                        config_dict_new[default_section][k] = v
                    else:
                        config_dict_new[default_section] = {k: v}

        resultant_stringio = StringIO()
        try:
            config.write(resultant_stringio)
            return resultant_stringio.getvalue(), config_dict_new
        finally:
            resultant_stringio.close()

    @staticmethod
    def _option_write(config, section, key, value):
        config.remove_option(str(section), str(key))
        try:
            if not any(list(value.values())):
                value = tuple(value.keys())
        except AttributeError:
            pass
        if isinstance(value, (tuple, set)):
            config.set(str(section), str(key), value)
        elif isinstance(value, set):
            config.set(str(section), str(key), value)
        elif isinstance(value, list):
            config.set(str(section), str(key), ','.join(str(i) for i in value))
        else:
            config.set(str(section), str(key), str(value))

    def return_config_overrides_json(self,
                                     config_overrides,
                                     resultant,
                                     list_extend=True,
                                     ignore_none_type=True,
                                     default_section='DEFAULT',
                                     yml_multilines=False):
        """Returns config json and dict of merged config

        Its important to note that file ordering will not be preserved as the
        information within the json file will be sorted by keys.

        :param config_overrides: ``dict``
        :param resultant: ``str`` || ``unicode``
        :returns: ``str``, ``dict``
        """
        original_resultant = json.loads(resultant)
        merged_resultant = self._merge_dict(
            base_items=original_resultant,
            new_items=config_overrides,
            list_extend=list_extend,
            yml_multilines=yml_multilines
        )
        return json.dumps(
            merged_resultant,
            indent=4,
            sort_keys=True
        ), merged_resultant

    def return_config_overrides_yaml(self,
                                     config_overrides,
                                     resultant,
                                     list_extend=True,
                                     ignore_none_type=True,
                                     default_section='DEFAULT',
                                     yml_multilines=False):
        """Return config yaml and dict of merged config

        :param config_overrides: ``dict``
        :param resultant: ``str`` || ``unicode``
        :returns: ``str``, ``dict``
        """
        original_resultant = yaml.safe_load(resultant)
        merged_resultant = self._merge_dict(
            base_items=original_resultant,
            new_items=config_overrides,
            list_extend=list_extend,
            yml_multilines=yml_multilines
        )
        return yaml.dump(
            merged_resultant,
            Dumper=IDumper,
            default_flow_style=False,
            width=1000,
        ), merged_resultant

    def _merge_dict(self,
                    base_items,
                    new_items,
                    list_extend=True,
                    yml_multilines=False):
        """Recursively merge new_items into base_items.

        :param base_items: ``dict``
        :param new_items: ``dict`` || ``list``
        :returns: ``dict``
        """
        if isinstance(new_items, dict):
            for key, value in new_items.items():
                if isinstance(value, dict):
                    base_items[key] = self._merge_dict(
                        base_items=base_items.get(key, {}),
                        new_items=value,
                        list_extend=list_extend
                    )
                elif (not isinstance(value, int) and (
                      ',' in value or (
                        '\n' in value and not yml_multilines))):
                    base_items[key] = re.split(',|\n', value)
                    base_items[key] = [
                        i.strip() for i in base_items[key] if i
                    ]
                elif isinstance(value, list):
                    if isinstance(base_items.get(key), list) and list_extend:
                        base_items[key].extend(value)
                    else:
                        base_items[key] = value
                elif isinstance(value, (tuple, set)):
                    le = list_extend  # assigned for pep8
                    if isinstance(base_items.get(key), tuple) and le:
                        base_items[key] += tuple(value)
                    elif isinstance(base_items.get(key), list) and le:
                        base_items[key].extend(list(value))
                    else:
                        base_items[key] = value
                else:
                    base_items[key] = new_items[key]
        elif isinstance(new_items, list):
            if list_extend:
                base_items.extend(new_items)
            else:
                base_items = new_items
        return base_items

    def _load_options_and_status(self, task_vars):
        """Return options and status from module load."""

        config_type = self._task.args.get('config_type')
        if config_type not in ['ini', 'yaml', 'json']:
            return False, dict(
                failed=True,
                msg="No valid [ config_type ] was provided. Valid options are"
                    " ini, yaml, or json."
            )

        # Access to protected method is unavoidable in Ansible
        searchpath = [self._loader._basedir]

        if self._task._role:
            file_path = self._task._role._role_path
            searchpath.insert(1, C.DEFAULT_ROLES_PATH)
            searchpath.insert(1, self._task._role._role_path)
        else:
            file_path = self._loader.get_basedir()

        user_source = self._task.args.get('src')
        remote_src = boolean(
            self._task.args.get('remote_src', False),
            strict=False
        )
        if remote_src:
            slurpee = self._execute_module(
                module_name='slurp',
                module_args=dict(src=user_source),
                task_vars=task_vars
            )
            _content = base64.b64decode(slurpee['content'])
            _user_content = _content.decode('utf-8')
        else:
            # (alextricity25) It's possible that the user could pass in a
            # datatype and not always a string. In this case we don't want
            # the datatype python representation to be printed out to the
            # file, but rather we want the serialized version.
            _user_content = self._task.args.get('content')

            # If the data type of the content input is a dictionary, it's
            # converted dumped as json if config_type is 'json'.
            if isinstance(_user_content, dict):
                if self._task.args.get('config_type') == 'json':
                    _user_content = json.dumps(_user_content)

        user_content = str(_user_content)
        if not user_source:
            if not user_content:
                return False, dict(
                    failed=True,
                    msg="No user [ src ] or [ content ] was provided"
                )
            else:
                tmp_content = None
                fd, tmp_content = tmpfilelib.mkstemp()
                try:
                    with open(tmp_content, 'wb') as f:
                        f.write(user_content.encode())
                except Exception as err:
                    os.remove(tmp_content)
                    raise Exception(err)
                self._task.args['src'] = source = tmp_content
        else:
            source = self._loader.path_dwim_relative(
                file_path,
                'templates',
                user_source
            )
        searchpath.insert(1, os.path.dirname(source))

        _dest = self._task.args.get('dest')
        list_extend = self._task.args.get('list_extend')
        if not _dest:
            return False, dict(
                failed=True,
                msg="No [ dest ] was provided"
            )
        else:
            # Expand any user home dir specification
            user_dest = self._remote_expand_user(_dest)
            if user_dest.endswith(os.sep):
                user_dest = os.path.join(user_dest, os.path.basename(source))

        # Get ignore_none_type
        # In some situations(i.e. my.cnf files), INI files can have valueless
        # options that don't have a '=' or ':' suffix. In these cases,
        # configparser gives these options a "None" value. If ignore_none_type
        # is set to true, these key/value options will be ignored, if it's set
        # to false, then ConfigTemplateParser will write out only the option
        # name with out the '=' or ':' suffix. The default is true.
        ignore_none_type = self._task.args.get('ignore_none_type', True)

        default_section = self._task.args.get('default_section', 'DEFAULT')
        remote_src = self._task.args.get('remote_src', False)

        yml_multilines = self._task.args.get('yml_multilines', False)
        block_end_string = self._task.args.get('block_end_string', '%}')
        block_start_string = self._task.args.get('block_start_string', '{%')
        variable_end_string = self._task.args.get('variable_end_string', '}}')
        variable_start_string = self._task.args.get('variable_start_string', '{{')

        return True, dict(
            source=source,
            dest=user_dest,
            config_overrides=self._task.args.get('config_overrides', {}),
            config_type=config_type,
            searchpath=searchpath,
            list_extend=list_extend,
            ignore_none_type=ignore_none_type,
            default_section=default_section,
            yml_multilines=yml_multilines,
            remote_src=remote_src,
            block_end_string=block_end_string,
            block_start_string=block_start_string,
            variable_end_string=variable_end_string,
            variable_start_string=variable_start_string
        )

    def resultant_ini_as_dict(self, resultant_dict, return_dict=None):
        if not return_dict:
            return_dict = {}

        for key, value in resultant_dict.items():
            if not value:
                continue
            key = key.split(STRIP_MARKER)[0]
            if isinstance(value, (OrderedDict, MultiKeyDict, dict)):
                return_dict[key] = self.resultant_ini_as_dict(value)
            else:
                return_dict[key] = value

        return return_dict

    def _check_templar(self, data, extra):
        if boolean(self._task.args.get('render_template', True)):
            templar = self._templar
            with templar.set_temporary_context(
                variable_start_string=extra['variable_start_string'],
                variable_end_string=extra['variable_end_string'],
                block_start_string=extra['block_start_string'],
                block_end_string=extra['block_end_string'],
                searchpath=extra['searchpath']
            ):
                return templar.template(
                    data,
                    preserve_trailing_newlines=True,
                    escape_backslashes=False,
                    convert_data=False
                )
        else:
            return data

    def run(self, tmp=None, task_vars=None):
        """Run the method"""

        if not tmp:
            try:
                remote_user = self._get_remote_user()
            except Exception:
                remote_user = task_vars.get('ansible_user')
                if not remote_user:
                    remote_user = task_vars.get('ansible_ssh_user')
                if not remote_user:
                    remote_user = self._play_context.remote_user
            try:
                tmp = self._make_tmp_path(remote_user)
            except TypeError:
                tmp = self._make_tmp_path()

        _status, _vars = self._load_options_and_status(task_vars=task_vars)
        if not _status:
            return _vars

        temp_vars = task_vars.copy()
        template_host = temp_vars['template_host'] = os.uname()[1]
        source = temp_vars['template_path'] = _vars['source']

        try:
            mtime = os.path.getmtime(source)
            temp_vars['template_mtime'] = datetime.datetime.fromtimestamp(
                mtime
            )
            try:
                template_uid = temp_vars['template_uid'] = pwd.getpwuid(
                    os.stat(source).st_uid
                ).pw_name
            except Exception:
                template_uid = temp_vars['template_uid'] = os.stat(
                    source
                ).st_uid
        except (PermissionError, FileNotFoundError):
            local_task_vars = temp_vars.copy()
            if not boolean(self._task.args.get('remote_src', False),
                           strict=False):
                local_task_vars['connection'] = 'local'
            stat = self._execute_module(
                module_name='stat',
                module_args=dict(path=source),
                task_vars=local_task_vars
            )
            mtime = stat['stat']['mtime']
            temp_vars['template_mtime'] = datetime.datetime.fromtimestamp(
                mtime
            )
            template_uid = stat['stat']['uid']

        managed_default = C.DEFAULT_MANAGED_STR
        managed_str = managed_default.format(
            host=template_host,
            uid=template_uid,
            file=to_bytes(source)
        )

        temp_vars['ansible_managed'] = time.strftime(
            managed_str,
            time.localtime(mtime)
        )
        temp_vars['template_fullpath'] = os.path.abspath(source)
        temp_vars['template_run_date'] = datetime.datetime.now()

        try:
            with open(source, 'r') as f:
                template_data = to_text(f.read())
        except (PermissionError, FileNotFoundError):
            local_temp_vars = task_vars.copy()
            if not boolean(self._task.args.get('remote_src', False),
                           strict=False):
                local_temp_vars['connection'] = 'local'
            template_data_slurpee = self._execute_module(
                module_name='slurp',
                module_args=dict(src=source),
                task_vars=local_temp_vars
            )
            template_data = base64.b64decode(
                template_data_slurpee['content']
            ).decode('utf-8')

        self._templar.available_variables = temp_vars

        if self._task.args.get('content'):
            resultant = template_data
        else:
            resultant = self._check_templar(data=template_data, extra=_vars)

        type_merger = getattr(self, CONFIG_TYPES.get(_vars['config_type']))
        resultant, config_base = type_merger(
            config_overrides=_vars['config_overrides'],
            resultant=resultant,
            list_extend=_vars.get('list_extend', True),
            ignore_none_type=_vars.get('ignore_none_type', True),
            default_section=_vars.get('default_section', 'DEFAULT'),
            yml_multilines=_vars.get('yml_multilines', False)
        )

        changed = False
        config_new = None
        if self._play_context.diff:
            slurpee = self._execute_module(
                module_name='slurp',
                module_args=dict(src=_vars['dest']),
                task_vars=task_vars
            )
            if 'content' in slurpee:
                dest_data = base64.b64decode(
                    slurpee['content']).decode('utf-8')
                resultant_dest = self._check_templar(data=dest_data, extra=_vars)
                type_merger = getattr(self,
                                      CONFIG_TYPES.get(_vars['config_type']))
                _, config_new = type_merger(
                    config_overrides={},
                    resultant=resultant_dest,
                    list_extend=_vars.get('list_extend', True),
                    ignore_none_type=_vars.get('ignore_none_type', True),
                    default_section=_vars.get('default_section', 'DEFAULT'),
                    yml_multilines=_vars.get('yml_multilines', False)
                )

            # Compare source+overrides with dest to look for changes and
            # build diff
            if isinstance(config_base, dict):
                if not config_new:
                    config_new = dict()
                cmp_dicts = DictCompare(
                    self.resultant_ini_as_dict(resultant_dict=config_new),
                    self.resultant_ini_as_dict(resultant_dict=config_base)
                )
                mods, changed = cmp_dicts.get_changes()
            elif isinstance(config_base, list):
                if not config_new:
                    config_new = list()
                mods = {
                    'added': [
                        i for i in config_new
                        if i not in config_base
                    ],
                    'removed': [
                        i for i in config_base
                        if i not in config_new
                    ],
                    'changed': [
                        i for i in (config_base + config_new)
                        if i not in config_base or i not in config_new
                    ]
                }
                changed = len(mods['changed']) > 0

        # run the copy module
        new_module_args = self._task.args.copy()
        # Access to protected method is unavoidable in Ansible
        transferred_data = self._transfer_data(
            self._connection._shell.join_path(tmp, 'source'),
            resultant
        )
        new_module_args.update(
            dict(
                src=transferred_data,
                dest=_vars['dest'],
                _original_basename=os.path.basename(source),
                follow=True,
            ),
        )

        # Remove data types that are not available to the copy module
        new_module_args.pop('config_overrides', None)
        new_module_args.pop('config_type', None)
        new_module_args.pop('list_extend', None)
        new_module_args.pop('ignore_none_type', None)
        new_module_args.pop('default_section', None)
        new_module_args.pop('yml_multilines', None)
        new_module_args.pop('block_end_string', None)
        new_module_args.pop('block_start_string', None)
        new_module_args.pop('variable_end_string', None)
        new_module_args.pop('variable_start_string', None)

        # While this is in the copy module we dont want to use it.
        new_module_args.pop('remote_src', None)

        # Content from config_template is converted to src
        new_module_args.pop('content', None)

        # remove render enablement option
        new_module_args.pop('render_template', None)

        # Run the copy module
        rc = self._execute_module(
            module_name='copy',
            module_args=new_module_args,
            task_vars=task_vars
        )
        copy_changed = rc.get('changed')
        if not copy_changed:
            rc['changed'] = changed

        if self._play_context.diff:
            rc['diff'] = []
            rc['diff'].append(
                {'prepared': json.dumps(mods, indent=4, sort_keys=True)})
        if self._task.args.get('content'):
            os.remove(_vars['source'])
        return rc

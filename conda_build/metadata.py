from __future__ import absolute_import, division, print_function

from collections import OrderedDict
import copy
import hashlib
import json
import os
from os.path import isfile, join
import re
import sys
import time

from bs4 import UnicodeDammit

from .conda_interface import iteritems, PY3, text_type
from .conda_interface import md5_file
from .conda_interface import non_x86_linux_machines
from .conda_interface import MatchSpec
from .conda_interface import specs_from_url
from .conda_interface import envs_dirs
from .conda_interface import string_types

from conda_build import exceptions, utils, variants
from conda_build.features import feature_list
from conda_build.config import Config, get_or_merge_config
from conda_build.utils import (ensure_list, find_recipe, expand_globs, get_installed_packages,
                               HashableDict, trim_empty_keys, filter_files, insert_variant_versions)
from conda_build.license_family import ensure_valid_license_family

try:
    import yaml

    # try to import C loader
    try:
        from yaml import CBaseLoader as BaseLoader
    except ImportError:
        from yaml import BaseLoader
except ImportError:
    sys.exit('Error: could not import yaml (required to read meta.yaml '
             'files of conda recipes)')

on_win = (sys.platform == 'win32')

# arches that don't follow exact names in the subdir need to be mapped here
ARCH_MAP = {'32': 'x86',
            '64': 'x86_64'}


def ns_cfg(config):
    # Remember to update the docs of any of this changes
    plat = config.host_subdir
    d = dict(
        linux=plat.startswith('linux-'),
        linux32=bool(plat == 'linux-32'),
        linux64=bool(plat == 'linux-64'),
        arm=plat.startswith('linux-arm'),
        osx=plat.startswith('osx-'),
        unix=plat.startswith(('linux-', 'osx-')),
        win=plat.startswith('win-'),
        win32=bool(plat == 'win-32'),
        win64=bool(plat == 'win-64'),
        x86=plat.endswith(('-32', '-64')),
        x86_64=plat.endswith('-64'),
        os=os,
        environ=os.environ,
        nomkl=bool(int(os.environ.get('FEATURE_NOMKL', False)))
    )

    defaults = variants.get_default_variants()[0]
    py = config.variant.get('python', defaults['python'])
    py = int("".join(py.split('.')[:2]))
    d.update(dict(py=py,
                    py3k=bool(30 <= py < 40),
                    py2k=bool(20 <= py < 30),
                    py26=bool(py == 26),
                    py27=bool(py == 27),
                    py33=bool(py == 33),
                    py34=bool(py == 34),
                    py35=bool(py == 35),
                    py36=bool(py == 36),))

    np = config.variant.get('numpy', defaults['numpy'])
    d['np'] = int("".join(np.split('.')[:2]))

    pl = config.variant.get('perl', defaults['perl'])
    d['pl'] = pl

    lua = config.variant.get('lua', defaults['lua'])
    d['lua'] = lua
    d['luajit'] = bool(lua[0] == "2")

    for machine in non_x86_linux_machines:
        d[machine] = bool(plat == 'linux-%s' % machine)

    for feature, value in feature_list:
        d[feature] = value
    d.update(os.environ)
    for k, v in config.variant.items():
        if k not in d:
            try:
                d[k] = int(v)
            except (TypeError, ValueError):
                d[k] = v
    return d


# Selectors must be either:
# - at end of the line
# - embedded (anywhere) within a comment
#
# Notes:
# - [([^\[\]]+)\] means "find a pair of brackets containing any
#                 NON-bracket chars, and capture the contents"
# - (?(2)[^\(\)]*)$ means "allow trailing characters iff group 2 (#.*) was found."
#                 Skip markdown link syntax.
sel_pat = re.compile(r'(.+?)\s*(#.*)?\[([^\[\]]+)\](?(2)[^\(\)]*)$')


# this function extracts the variable name from a NameError exception, it has the form of:
# "NameError: name 'var' is not defined", where var is the variable that is not defined. This gets
#    returned
def parseNameNotFound(error):
    m = re.search('\'(.+?)\'', str(error))
    if len(m.groups()) == 1:
        return m.group(1)
    else:
        return ""


# We evaluate the selector and return True (keep this line) or False (drop this line)
# If we encounter a NameError (unknown variable in selector), then we replace it by False and
#     re-run the evaluation
def eval_selector(selector_string, namespace, variants_in_place):
    try:
        # TODO: is there a way to do this without eval?  Eval allows arbitrary
        #    code execution.
        return eval(selector_string, namespace, {})
    except NameError as e:
        missing_var = parseNameNotFound(e)
        if variants_in_place:
            print("Warning: Treating unknown selector \'" + missing_var + "\' as if it was False.")
        next_string = selector_string.replace(missing_var, "False")
        return eval_selector(next_string, namespace, variants_in_place)


def select_lines(data, namespace, variants_in_place):
    lines = []

    for i, line in enumerate(data.splitlines()):
        line = line.rstrip()

        trailing_quote = ""
        if line and line[-1] in ("'", '"'):
            trailing_quote = line[-1]

        if line.lstrip().startswith('#'):
            # Don't bother with comment only lines
            continue
        m = sel_pat.match(line)
        if m:
            cond = m.group(3)
            try:
                if eval_selector(cond, namespace, variants_in_place):
                    lines.append(m.group(1) + trailing_quote)
            except Exception as e:
                sys.exit('''\
Error: Invalid selector in meta.yaml line %d:
offending line:
%s
exception:
%s
''' % (i + 1, line, str(e)))
        else:
            lines.append(line)
    return '\n'.join(lines) + '\n'


def yamlize(data):
    try:
        return yaml.load(data, Loader=BaseLoader)
    except yaml.error.YAMLError as e:
        if '{{' in data:
            try:
                import jinja2
                jinja2  # Avoid pyflakes failure: 'jinja2' imported but unused
            except ImportError:
                raise exceptions.UnableToParseMissingJinja2(original=e)
        raise exceptions.UnableToParse(original=e)


def ensure_valid_fields(meta):
    pin_depends = meta.get('build', {}).get('pin_depends', '')
    if pin_depends and pin_depends not in ('', 'record', 'strict'):
        raise RuntimeError("build/pin_depends must be 'record' or 'strict' - "
                           "not '%s'" % pin_depends)


def _trim_None_strings(meta_dict):
    log = utils.get_logger(__name__)
    for key, value in meta_dict.items():
        if hasattr(value, 'keys'):
            meta_dict[key] = _trim_None_strings(value)
        elif value and hasattr(value, '__iter__') or isinstance(value, string_types):
            if isinstance(value, string_types):
                meta_dict[key] = None if 'None' in value else value
            else:
                # support lists of dicts (homogeneous)
                keep = []
                if hasattr(next(iter(value)), 'keys'):
                    for d in value:
                        trimmed_dict = _trim_None_strings(d)
                        if trimmed_dict:
                            keep.append(trimmed_dict)
                # support lists of strings (homogeneous)
                else:
                    keep = [i for i in value if 'None' not in i]
                meta_dict[key] = keep
        else:
            log.debug("found unrecognized data type in dictionary: {0}, type: {1}".format(value,
                                                                                    type(value)))
    trim_empty_keys(meta_dict)
    return meta_dict


def ensure_valid_noarch_value(meta):
    try:
        build_noarch = meta['build']['noarch']
    except KeyError:
        return
    if build_noarch.lower() == 'none':
        raise exceptions.CondaBuildException("Invalid value for noarch: %s" % build_noarch)


def _get_all_dependencies(metadata, envs=('host', 'build', 'run')):
    reqs = []
    for _env in envs:
        reqs.extend(metadata.meta.get('requirements', {}).get(_env, []))
    return reqs


def check_circular_dependencies(render_order):
    pairs = []
    for idx, m in enumerate(render_order.values()):
        for other_m in list(render_order.values())[idx + 1:]:
            if (any(m.name() == dep or dep.startswith(m.name() + ' ')
                   for dep in _get_all_dependencies(other_m)) and
                any(other_m.name() == dep or dep.startswith(other_m.name() + ' ')
                   for dep in _get_all_dependencies(m))):
                pairs.append((m.name(), other_m.name()))
    if pairs:
        error = "Circular dependencies in recipe: \n"
        for pair in pairs:
            error += "    {0} <-> {1}\n".format(*pair)
        raise exceptions.RecipeError(error)


def ensure_matching_hashes(output_metadata):
    envs = 'build', 'host', 'run'
    problemos = []
    for (_, m) in output_metadata.values():
        for (_, om) in output_metadata.values():
            if m != om:
                run_exports = om.meta.get('build', {}).get('run_exports', [])
                if hasattr(run_exports, 'keys'):
                    run_exports = run_exports.get('strong', []) + run_exports.get('weak', [])
                deps = _get_all_dependencies(om, envs) + run_exports
                for dep in deps:
                    if (dep.startswith(m.name() + ' ') and len(dep.split(' ')) == 3 and
                            dep.split(' ')[-1] != m.build_id()):
                        problemos.append((m.name(), om.name()))

    if problemos:
        error = ""
        for prob in problemos:
            error += "Mismatching package: {}; consumer package: {}\n".format(*prob)
        raise exceptions.RecipeError("Mismatching hashes in recipe. Exact pins in dependencies "
                                     "that contribute to the hash often cause this. Can you "
                                     "change one or more exact pins to version bound constraints?\n"
                                     "Involved packages were:\n" + error)


def parse(data, config, path=None):
    data = select_lines(data, ns_cfg(config), variants_in_place=bool(config.variant))
    res = yamlize(data)
    # ensure the result is a dict
    if res is None:
        res = {}
    for field in FIELDS:
        if field not in res:
            continue
        # ensure that empty fields are dicts (otherwise selectors can cause invalid fields)
        if not res[field]:
            res[field] = {}
        # source field may be either a dictionary, or a list of dictionaries
        if field == 'source':
            if not (isinstance(res[field], dict) or (hasattr(res[field], '__iter__') and not
                        isinstance(res[field], string_types))):
                raise RuntimeError("The %s field should be a dict or list of dicts, not "
                                   "%s in file %s." % (field, res[field].__class__.__name__, path))
        else:
            if not isinstance(res[field], dict):
                raise RuntimeError("The %s field should be a dict, not %s in file %s." %
                                (field, res[field].__class__.__name__, path))

    ensure_valid_fields(res)
    ensure_valid_license_family(res)
    ensure_valid_noarch_value(res)
    return sanitize(res)


trues = {'y', 'on', 'true', 'yes'}
falses = {'n', 'no', 'false', 'off'}

default_structs = {
    'build/entry_points': list,
    'build/features': list,
    'source/patches': list,
    'build/script': list,
    'build/script_env': list,
    'build/run_exports': list,
    'build/track_features': list,
    'build/osx_is_app': bool,
    'build/preserve_egg_dir': bool,
    'build/binary_relocation': bool,
    'build/noarch': text_type,
    'build/noarch_python': bool,
    'build/detect_binary_files_with_prefix': bool,
    'build/skip': bool,
    'build/skip_compile_pyc': list,
    'build/preferred_env': text_type,
    'build/preferred_env_executable_paths': list,
    'build/ignore_run_exports': list,
    'build/requires_features': dict,
    'build/provides_features': dict,
    'requirements/build': list,
    'requirements/host': list,
    'requirements/run': list,
    'requirements/conflicts': list,
    'requirements/run_constrained': list,
    'test/requires': list,
    'test/files': list,
    'test/source_files': list,
    'test/commands': list,
    'test/imports': list,
    'package/version': text_type,
    'build/string': text_type,
    'build/pin_depends': text_type,
    'source/svn_rev': text_type,
    'source/git_tag': text_type,
    'source/git_branch': text_type,
    'source/md5': text_type,
    'source/git_rev': text_type,
    'source/path': text_type,
    'source/git_url': text_type,
    'app/own_environment': bool
}


def sanitize(meta):
    """
    Sanitize the meta-data to remove aliases/handle deprecation

    """
    sanitize_funs = {'source': [_git_clean]}
    for section, funs in sanitize_funs.items():
        if section in meta:
            for func in funs:
                section_data = meta[section]
                # section is a dictionary
                if hasattr(section_data, 'keys'):
                    section_data = func(section_data)
                # section is a list of dictionaries
                else:
                    section_data = [func(_d) for _d in section_data]
                meta[section] = section_data
    _trim_None_strings(meta)
    return meta


def _git_clean(source_meta):
    """
    Reduce the redundancy in git specification by removing git_tag and
    git_branch.

    If one is specified, copy to git_rev.

    If more than one field is used to specified, exit
    and complain.
    """

    git_rev_tags_old = ('git_branch', 'git_tag')
    git_rev = 'git_rev'

    git_rev_tags = (git_rev,) + git_rev_tags_old

    has_rev_tags = tuple(bool(source_meta.get(tag, text_type())) for
                          tag in git_rev_tags)
    if sum(has_rev_tags) > 1:
        msg = "Error: multiple git_revs:"
        msg += ', '.join("{}".format(key) for key, has in
                         zip(git_rev_tags, has_rev_tags) if has)
        sys.exit(msg)

    # make a copy of the input so we have no side-effects
    ret_meta = source_meta.copy()
    # loop over the old versions
    for key, has in zip(git_rev_tags[1:], has_rev_tags[1:]):
        # update if needed
        if has:
            ret_meta[git_rev_tags[0]] = ret_meta[key]
        # and remove
        ret_meta.pop(key, None)

    return ret_meta


# If you update this please update the example in
# conda-docs/docs/source/build.rst
FIELDS = {
    'package': {'name', 'version'},
    'source': {'fn', 'url', 'md5', 'sha1', 'sha256', 'path',
               'git_url', 'git_tag', 'git_branch', 'git_rev', 'git_depth',
               'hg_url', 'hg_tag',
               'svn_url', 'svn_rev', 'svn_ignore_externals',
               'patches'
               },
    'build': {'number', 'string', 'entry_points', 'osx_is_app',
              'features', 'track_features', 'preserve_egg_dir',
              'no_link', 'binary_relocation', 'script', 'noarch', 'noarch_python',
              'has_prefix_files', 'binary_has_prefix_files', 'ignore_prefix_files',
              'detect_binary_files_with_prefix', 'skip_compile_pyc', 'rpaths',
              'script_env', 'always_include_files', 'skip', 'msvc_compiler',
              'pin_depends', 'include_recipe',  # pin_depends is experimental still
              'preferred_env', 'preferred_env_executable_paths', 'run_exports',
              'ignore_run_exports', 'requires_features', 'provides_features',
              },
    'requirements': {'build', 'host', 'run', 'conflicts', 'run_constrained'},
    'app': {'entry', 'icon', 'summary', 'type', 'cli_opts',
            'own_environment'},
    'test': {'requires', 'commands', 'files', 'imports', 'source_files'},
    'about': {'home', 'dev_url', 'doc_url', 'doc_source_url', 'license_url',  # these are URLs
              'license', 'summary', 'description', 'license_family',  # text
              'license_file', 'readme',  # paths in source tree
              },
}


def check_bad_chrs(s, field):
    bad_chrs = '=@#$%^&*:;"\'\\|<>?/ '
    if field in ('package/version', 'build/string'):
        bad_chrs += '-'
    if field != 'package/version':
        bad_chrs += '!'
    for c in bad_chrs:
        if c in s:
            sys.exit("Error: bad character '%s' in %s: %s" % (c, field, s))


def build_string_from_metadata(metadata):
    if metadata.meta.get('build', {}).get('string'):
        build_str = metadata.get_value('build/string')
    else:
        res = []
        build_or_host = 'host' if metadata.is_cross else 'build'
        build_pkg_names = [ms.name for ms in metadata.ms_depends(build_or_host)]
        # TODO: this is the bit that puts in strings like py27np111 in the filename.  It would be
        #    nice to get rid of this, since the hash supercedes that functionally, but not clear
        #    whether anyone's tools depend on this file naming right now.
        for s, names, places in (('np', 'numpy', 2), ('py', 'python', 2), ('pl', 'perl', 2),
                                 ('lua', 'lua', 2), ('r', ('r', 'r-base'), 3)):
            for ms in metadata.ms_depends('run'):
                for name in ensure_list(names):
                    if ms.name == name and name in build_pkg_names:
                        # only append numpy when it is actually pinned
                        if name == 'numpy' and not metadata.numpy_xx:
                            continue
                        if metadata.noarch == name or (metadata.get_value('build/noarch_python') and
                                                    name == 'python'):
                            res.append(s)
                        else:
                            pkg_names = list(ensure_list(names))
                            pkg_names.extend([_n.replace('-', '_')
                                              for _n in ensure_list(names) if '-' in _n])
                            for _n in pkg_names:
                                _n = _n.replace('-', '_')
                                variant_version = metadata.config.variant.get(_n, "")
                                if variant_version:
                                    break
                            res.append(''.join([s] + variant_version.split('.')[:places]))

        features = ensure_list(metadata.get_value('build/features', []))
        if res:
            res.append('_')
        if features:
            res.extend(('_'.join(features), '_'))
        res.append('{0}'.format(metadata.build_number() if metadata.build_number() else 0))
        build_str = "".join(res)
    return build_str


# This really belongs in conda, and it is int conda.cli.common,
#   but we don't presently have an API there.
def _get_env_path(env_name_or_path):
    if not os.path.isdir(env_name_or_path):
        for envs_dir in list(envs_dirs) + [os.getcwd()]:
            path = os.path.join(envs_dir, env_name_or_path)
            if os.path.isdir(path):
                env_name_or_path = path
                break
    bootstrap_metadir = os.path.join(env_name_or_path, 'conda-meta')
    if not os.path.isdir(bootstrap_metadir):
        print("Bootstrap environment '%s' not found" % env_name_or_path)
        sys.exit(1)
    return env_name_or_path


def _get_dependencies_from_environment(env_name_or_path):
    path = _get_env_path(env_name_or_path)
    # construct build requirements that replicate the given bootstrap environment
    # and concatenate them to the build requirements from the recipe
    bootstrap_metadata = get_installed_packages(path)
    bootstrap_requirements = []
    for package, data in bootstrap_metadata.items():
        bootstrap_requirements.append("%s %s %s" % (package, data['version'], data['build']))
    return {'requirements': {'build': bootstrap_requirements}}


def toposort(output_metadata_map, phase):
    '''This function is used to work out the order to run the install scripts
       for split packages based on any interdependencies. The result is just
       a re-ordering of outputs such that we can run them in that order and
       reset the initial set of files in the install prefix after each. This
       will naturally lead to non-overlapping files in each package and also
       the correct files being present during the install and test procedures,
       provided they are run in this order.'''
    from .conda_interface import _toposort
    # We only care about the conda packages built by this recipe. Non-conda
    # packages get sorted to the end.
    these_packages = [output_d['name'] for output_d in output_metadata_map
                      if output_d.get('type', 'conda') == 'conda']
    topodict = dict()
    order = dict()
    endorder = set()
    for idx, (output_d, output_m) in enumerate(output_metadata_map.items()):
        if output_d.get('type', 'conda') == 'conda':
            name = output_d['name']
            order[name] = idx
            topodict[name] = set()
            for run_dep in output_m.get_value('requirements/{}'.format(phase), []):
                run_dep = run_dep.split(' ')[0]
                if run_dep in these_packages:
                    topodict[name].update((run_dep,))
        else:
            endorder.add(idx)
    topo_order = list(_toposort(topodict))
    keys = [k for pkgname in topo_order for k in output_metadata_map.keys()
            if 'name' in k and k['name'] == pkgname]
    # not sure that this is working...  not everything has 'name', and not sure how this pans out
    #    may end up excluding packages without the 'name' field
    keys.extend([k for pkgname in endorder for k in output_metadata_map.keys()
                 if ('name' in k and k['name'] == pkgname) or 'name' not in k])
    result = OrderedDict()
    for key in keys:
        result[key] = output_metadata_map[key]
    return result


def output_dict_from_top_level_meta(m):
    requirements = m.meta.get('requirements', {})
    output_d = {'name': m.name(), 'requirements': requirements,
                'noarch_python': m.get_value('build/noarch_python'),
                'noarch': m.get_value('build/noarch'),
                'type': 'conda',
                }

    run_exports = m.meta.get('build', {}).get('run_exports')
    if run_exports:
        output_d['run_exports'] = run_exports
    return output_d


def get_output_dicts_from_metadata(metadata):
    outputs = metadata.get_section('outputs')

    if not outputs:
        outputs = [output_dict_from_top_level_meta(metadata)]
    else:
        assert not hasattr(outputs, 'keys'), ('outputs specified as dictionary, but must be a '
                                              'list of dictionaries.  YAML syntax is: \n\n'
                                              'outputs:\n    - name: subpkg\n\n'
                                              '(note the - before the inner dictionary)')
        # make a metapackage for the top-level package if the top-level requirements
        #     mention a subpackage,
        # but only if a matching output name is not explicitly provided
        if metadata.uses_subpackage and not any(metadata.name() == out.get('name', '')
                                            for out in outputs):
            outputs.append(output_dict_from_top_level_meta(metadata))
    for out in outputs:
        if (metadata.name() == out.get('name', '') and not (out.get('files') or
                                                            out.get('script'))):
            out['requirements'] = out.get('requirements', metadata.meta.get('requirements', {}))
            out['noarch_python'] = out.get('noarch_python',
                                            metadata.get_value('build/noarch_python'))
            out['noarch'] = out.get('noarch', metadata.get_value('build/noarch'))
    return outputs


def finalize_outputs_pass(base_metadata, render_order, pass_no, outputs=None,
                          permit_unsatisfiable_variants=False):
    from .render import finalize_metadata
    outputs = OrderedDict()
    # each of these outputs can have a different set of dependency versions from each other,
    #    but also from base_metadata
    for output_d, metadata in render_order.values():
        try:
            log = utils.get_logger(__name__)
            # We should reparse the top-level recipe to get all of our dependencies fixed up.
            # we base things on base_metadata because it has the record of the full origin recipe
            if base_metadata.config.verbose:
                log.info("Attempting to finalize metadata for {}".format(metadata.name()))
            if pass_no == 0:
                base_metadata.original_meta = base_metadata.meta.copy()
            om = base_metadata.copy()
            # store a copy of the metadata before finalization, so that we know what is
            #     original stuff.  This is especially important for only applying run_exports
            #     to things that are actually specified in the recipe, not installed as deps.
            # match up the old variant with the current one from this output
            om.config.variant = metadata.config.variant
            if not hasattr(om, 'other_outputs'):
                om.other_outputs = OrderedDict()
            om.other_outputs.update(outputs)
            om.final = False
            om.parse_until_resolved()
            # get the new output_d from the reparsed top-level metadata, so that we have any
            #    exact subpackage version/hash info
            recipe_outputs = get_output_dicts_from_metadata(om)
            output_d = get_updated_output_dict_from_reparsed_metadata(output_d,
                                                                      recipe_outputs)
            om = om.get_output_metadata(output_d)
            fm = finalize_metadata(om, permit_unsatisfiable_variants=permit_unsatisfiable_variants)
            if not output_d.get('type') or output_d.get('type') == 'conda':
                outputs[(fm.name(), HashableDict(fm.config.variant))] = (output_d, fm)
        except exceptions.DependencyNeedsBuildingError as e:
            if not permit_unsatisfiable_variants:
                raise
            else:
                log = utils.get_logger(__name__)
                log.warn("Could not finalize metadata due to missing dependencies: "
                            "{}".format(e.packages))
                outputs[(metadata.name(), HashableDict(metadata.config.variant))] = (
                    output_d, metadata)
    base_metadata.other_outputs = outputs
    base_metadata.final = False
    base_metadata.parse_until_resolved()
    # in the final pass, we finalize each of the outputs.  Up to now, we have not been computing
    #  the actual installed versions of their dependencies (aside from subpackages)
    if pass_no == 2:
        final_outputs = OrderedDict()
        for k, (out_d, m) in outputs.items():
            fm = finalize_metadata(m, permit_unsatisfiable_variants=permit_unsatisfiable_variants)
            final_outputs[(m.name(), HashableDict(m.config.variant))] = out_d, fm
        return final_outputs
    else:
        return finalize_outputs_pass(base_metadata, render_order, pass_no + 1, outputs,
                                     permit_unsatisfiable_variants)


def get_updated_output_dict_from_reparsed_metadata(original_dict, new_outputs):
    output_d = original_dict
    if 'name' in original_dict:
        output_ds = [out for out in new_outputs if 'name' in out and
                    out['name'] == original_dict['name']]
        assert len(output_ds) == 1
        output_d = output_ds[0]
    return output_d


class MetaData(object):
    def __init__(self, path, config=None, variant=None):

        self.undefined_jinja_vars = []
        self.config = get_or_merge_config(config, variant=variant)

        if isfile(path):
            self.meta_path = path
            self.path = os.path.dirname(path)
        else:
            self.meta_path = find_recipe(path)
            self.path = os.path.dirname(self.meta_path)
        self.requirements_path = join(self.path, 'requirements.txt')

        # Start with bare-minimum contents so we can call environ.get_dict() with impunity
        # We'll immediately replace these contents in parse_again()
        self.meta = dict()

        # This is the 'first pass' parse of meta.yaml, so not all variables are defined yet
        # (e.g. GIT_FULL_HASH, etc. are undefined)
        # Therefore, undefined jinja variables are permitted here
        # In the second pass, we'll be more strict. See build.build()
        # Primarily for debugging.  Ensure that metadata is not altered after "finalizing"
        self.parse_again(permit_undefined_jinja=True, allow_no_other_outputs=True)
        self.config.disable_pip = self.disable_pip

    @property
    def is_cross(self):
        return bool(self.get_value('requirements/host'))

    @property
    def final(self):
        return self.get_value('extra/final')

    @final.setter
    def final(self, boolean):
        extra = self.meta.get('extra', {})
        extra['final'] = boolean
        self.meta['extra'] = extra

    @property
    def disable_pip(self):
        return self.config.disable_pip or ('build' in self.meta and
                                           'disable_pip' in self.meta['build'])

    @disable_pip.setter
    def disable_pip(self, value):
        self.config.disable_pip = value
        build = self.meta.get('build', {})
        build['disable_pip'] = value
        self.meta['build'] = build

    def append_metadata_sections(self, sections_file_or_dict, merge, raise_on_clobber=False):
        """Append to or replace subsections to meta.yaml

        This is used to alter input recipes, so that a given requirement or
        setting is applied without manually altering the input recipe. It is
        intended for vendors who want to extend existing recipes without
        necessarily removing information. pass merge=False to replace sections.
        """
        if hasattr(sections_file_or_dict, 'keys'):
            build_config = sections_file_or_dict
        else:
            with open(sections_file_or_dict) as configfile:
                build_config = parse(configfile.read(), config=self.config)
        utils.merge_or_update_dict(self.meta, build_config, self.path, merge=merge,
                                   raise_on_clobber=raise_on_clobber)

    def parse_again(self, permit_undefined_jinja=False, allow_no_other_outputs=False,
                    bypass_env_check=False):
        """Redo parsing for key-value pairs that are not initialized in the
        first pass.

        config: a conda-build Config object.  If None, the config object passed at creation
                time is used.

        permit_undefined_jinja: If True, *any* use of undefined jinja variables will
                                evaluate to an emtpy string, without emitting an error.
        """
        assert not self.final, "modifying metadata after finalization"

        log = utils.get_logger(__name__)

        if isfile(self.requirements_path) and not self.get_value('requirements/run'):
            self.meta.setdefault('requirements', {})
            run_requirements = specs_from_url(self.requirements_path)
            self.meta['requirements']['run'] = run_requirements

        os.environ["CONDA_BUILD_STATE"] = "RENDER"
        append_sections_file = None
        clobber_sections_file = None
        try:
            # we sometimes create metadata from dictionaries, in which case we'll have no path
            if self.meta_path:
                self.meta = parse(self._get_contents(permit_undefined_jinja,
                                                     allow_no_other_outputs=allow_no_other_outputs,
                                                     bypass_env_check=bypass_env_check),
                                  config=self.config,
                                  path=self.meta_path)

                if (isfile(self.requirements_path) and
                        not self.meta.get('requirements', {}).get('run', [])):
                    self.meta.setdefault('requirements', {})
                    run_requirements = specs_from_url(self.requirements_path)
                    self.meta['requirements']['run'] = run_requirements

                append_sections_file = os.path.join(self.path, 'recipe_append.yaml')
                clobber_sections_file = os.path.join(self.path, 'recipe_clobber.yaml')

            append_sections_file = self.config.append_sections_file or append_sections_file
            if append_sections_file and not os.path.isfile(append_sections_file):
                log.debug('input append sections file did not exist: %s', append_sections_file)
                append_sections_file = None
            clobber_sections_file = self.config.clobber_sections_file or clobber_sections_file
            if clobber_sections_file and not os.path.isfile(clobber_sections_file):
                log.debug('input clobber sections file did not exist: %s', clobber_sections_file)
                clobber_sections_file = None

            if append_sections_file:
                self.append_metadata_sections(append_sections_file, merge=True)
            if clobber_sections_file:
                self.append_metadata_sections(clobber_sections_file, merge=False)
            if self.config.bootstrap:
                dependencies = _get_dependencies_from_environment(self.config.bootstrap)
                self.append_metadata_sections(dependencies, merge=True)
        except:
            raise
        finally:
            del os.environ["CONDA_BUILD_STATE"]
            pass
        self.validate_features()
        self.ensure_no_pip_requirements()

    def ensure_no_pip_requirements(self):
        keys = 'requirements/build', 'requirements/run', 'test/requires'
        for key in keys:
            if any(hasattr(item, 'keys') for item in self.get_value(key)):
                raise ValueError("Dictionaries are not supported as values in requirements sections"
                                 ".  Note that pip requirements as used in conda-env "
                                 "environment.yml files are not supported by conda-build.")
        self.append_requirements()

    def append_requirements(self):
        """For dynamic determination of build or run reqs, based on configuration"""
        reqs = self.meta.get('requirements', {})
        run_reqs = reqs.get('run', [])
        if bool(self.get_value('build/osx_is_app', False)) and self.config.platform == 'osx':
            if 'python.app' not in run_reqs:
                run_reqs.append('python.app')
        self.meta['requirements'] = reqs

    def parse_until_resolved(self, allow_no_other_outputs=False, bypass_env_check=False):
        """variant contains key-value mapping for additional functions and values
        for jinja2 variables"""
        # undefined_jinja_vars is refreshed by self.parse again
        undefined_jinja_vars = ()
        # store the "final" state that we think we're in.  reloading the meta.yaml file
        #   can reset it (to True)
        final = self.final
        # always parse again at least once.
        self.parse_again(permit_undefined_jinja=True, allow_no_other_outputs=allow_no_other_outputs,
                         bypass_env_check=bypass_env_check)
        self.final = final

        while set(undefined_jinja_vars) != set(self.undefined_jinja_vars):
            undefined_jinja_vars = self.undefined_jinja_vars
            self.parse_again(permit_undefined_jinja=True,
                             allow_no_other_outputs=allow_no_other_outputs,
                             bypass_env_check=bypass_env_check)
            self.final = final
        if undefined_jinja_vars:
            sys.exit("Undefined Jinja2 variables remain ({}).  Please enable "
                     "source downloading and try again.".format(self.undefined_jinja_vars))

        # always parse again at the end, too.
        self.parse_again(permit_undefined_jinja=False,
                         allow_no_other_outputs=allow_no_other_outputs,
                         bypass_env_check=bypass_env_check)
        self.final = final

    @classmethod
    def fromstring(cls, metadata, config=None, variant=None):
        m = super(MetaData, cls).__new__(cls)
        if not config:
            config = Config()
        m.meta = parse(metadata, config=config, path='', variant=variant)
        m.config = config
        m.parse_again(permit_undefined_jinja=True)
        return m

    @classmethod
    def fromdict(cls, metadata, config=None, variant=None):
        """
        Create a MetaData object from metadata dict directly.
        """
        m = super(MetaData, cls).__new__(cls)
        m.path = ''
        m.meta_path = ''
        m.requirements_path = ''
        m.meta = sanitize(metadata)

        if not config:
            config = Config(variant=variant)

        m.config = config
        m.undefined_jinja_vars = []
        m.final = False

        return m

    def get_section(self, section):
        return self.meta.get(section, {})

    def get_value(self, name, default=None, autotype=True):
        """
        Get a value from a meta.yaml.
        :param field: Field to return, e.g. 'package/name'.
                      If the section might be a list, specify an index,
                      e.g. 'source/0/git_url'.
        :param default: Default object to return if field doesn't exist
        :param autotype: If True, return the default type of field if one exists.
                         False will return the default object.
        :return: The named value from meta.yaml
        """
        names = name.split('/')
        assert len(names) in (2, 3), "Bad field name: " + name
        if len(names) == 2:
            section, key = names
            index = None
        elif len(names) == 3:
            section, index, key = names
            assert section == 'source', "Section is not a list: " + section
            index = int(index)

        # get correct default
        field = section + '/' + key
        if autotype and default is None and field in default_structs:
            default = default_structs[field]()

        section_data = self.get_section(section)
        if isinstance(section_data, dict):
            assert not index, \
                "Got non-zero index ({}), but section {} is not a list.".format(index, section)
        elif isinstance(section_data, list):
            # The 'source' section can be written a list, in which case the name
            # is passed in with an index, e.g. get_value('source/0/git_url')
            if index is None:
                log = utils.get_logger(__name__)
                log.warn("No index specified in get_value('{}'). Assuming index 0.".format(name))
                index = 0

            if len(section_data) == 0:
                section_data = {}
            else:
                section_data = section_data[index]
                assert isinstance(section_data, dict), \
                    "Expected {}/{} to be a dict".format(section, index)

        value = section_data.get(key, default)

        # handle yaml 1.1 boolean values
        if isinstance(value, text_type):
            if value.lower() in trues:
                value = True
            elif value.lower() in falses:
                value = False

        return value

    def check_fields(self):
        for section, submeta in iteritems(self.meta):
            # anything goes in the extra section
            if section == 'extra':
                continue
            if section not in FIELDS:
                raise ValueError("unknown section: %s" % section)
            for key in submeta:
                if key not in FIELDS[section]:
                    raise ValueError("in section %r: unknown key %r" %
                             (section, key))
        return True

    def name(self):
        res = self.get_value('package/name')
        if not res:
            sys.exit('Error: package/name missing in: %r' % self.meta_path)
        res = text_type(res)
        if res != res.lower():
            sys.exit('Error: package/name must be lowercase, got: %r' % res)
        check_bad_chrs(res, 'package/name')
        return res

    def version(self):
        res = str(self.get_value('package/version'))
        if res is None:
            sys.exit("Error: package/version missing in: %r" % self.meta_path)
        check_bad_chrs(res, 'package/version')
        if self.final and res.startswith('.'):
            raise ValueError("Fully-rendered version can't start with period -  got %s", res)
        return res

    def build_number(self):
        number = self.get_value('build/number')
        # build number can come back as None if no setting (or jinja intermediate)
        try:
            build_int = int(number)
        except (ValueError, TypeError):
            build_int = ""
        return build_int

    def ms_depends(self, typ='run'):
        res = []
        names = ('python', 'numpy', 'perl', 'lua')
        name_ver_list = [(name, self.config.variant[name])
                         for name in names
                         if self.config.variant.get(name)]
        if self.config.variant.get('r_base'):
            # r is kept for legacy installations, r-base deprecates it.
            name_ver_list.extend([('r', self.config.variant['r_base']),
                                  ('r-base', self.config.variant['r_base']),
                                  ])
        for spec in self.get_value('requirements/' + typ, []):
            try:
                ms = MatchSpec(spec)
            except AssertionError:
                raise RuntimeError("Invalid package specification: %r" % spec)
            except (AttributeError, ValueError):
                raise RuntimeError("Received dictionary as spec.  Note that pip requirements are "
                                   "not supported in conda-build meta.yaml.")
            if ms.name == self.name():
                raise RuntimeError("%s cannot depend on itself" % self.name())
            for name, ver in name_ver_list:
                if ms.name == name:
                    if self.noarch:
                        continue

            for c in '=!@#$%^&*:;"\'\\|<>?/':
                if c in ms.name:
                    sys.exit("Error: bad character '%s' in package name "
                             "dependency '%s'" % (c, ms.name))
            parts = spec.split()
            if len(parts) >= 2:
                if parts[1] in {'>', '>=', '=', '==', '!=', '<', '<='}:
                    msg = ("Error: bad character '%s' in package version "
                           "dependency '%s'" % (parts[1], ms.name))
                    if len(parts) >= 3:
                        msg += "\nPerhaps you meant '%s %s%s'" % (ms.name,
                            parts[1], parts[2])
                    sys.exit(msg)
            res.append(ms)
        return res

    def get_hash_contents(self):
        sections = ['requirements', 'build']
        # make a copy of values, so that no sorting occurs in place
        composite = HashableDict({section: copy.copy(self.get_section(section))
                                  for section in sections})

        if self.get_section('source'):
            src = self.get_section('source')
            if hasattr(src, 'keys'):
                composite['source'] = HashableDict(src.copy())
            else:
                composite['source'] = [HashableDict(s.copy()) for s in src]

        # filter build requirements for ones that should not be in the hash
        requirements = composite.get('requirements', {})
        build_reqs = requirements.get('build', [])
        excludes = self.config.variant.get('ignore_version', [])
        if excludes:
            exclude_pattern = re.compile('|'.join('{}[\s$]?.*'.format(exc) for exc in excludes))
            build_reqs = [req for req in build_reqs if not exclude_pattern.match(req)]
        requirements['build'] = build_reqs
        composite['requirements'] = requirements
        if 'copy_test_source_files' in self.meta.get('extra', {}):
            composite['extra'] = HashableDict({'copy_test_source_files':
                            str(self.meta['extra']['copy_test_source_files']).lower() == 'true'})

        # remove the build number from the hash, so that we can bump it without changing the hash
        if 'number' in composite['build']:
            del composite['build']['number']
        # remove the build string, so that hashes don't affect themselves
        for key in ('string', 'noarch', 'noarch_python'):
            if key in composite['build']:
                del composite['build'][key]
        if not composite['build']:
            del composite['build']

        for key in 'build', 'run':
            if key in composite['requirements'] and not composite['requirements'].get(key):
                del composite['requirements'][key]

        file_paths = []
        if self.config.include_recipe and self.include_recipe():
            recorded_input_files = os.path.join(self.path, '..', 'hash_input_files')
            if os.path.exists(recorded_input_files):
                with open(recorded_input_files) as f:
                    file_paths = f.read().splitlines()
            else:
                # is this a subpackage?  If so, add only the files relevant to exactly this
                if 'parent_recipe' in self.meta.get('extra', {}):
                    parent_recipe_path = self.meta['extra']['parent_recipe'].get('path')
                    outputs = self.meta.get('outputs', [])
                    this_output = [out for out in outputs if out.get('name') == self.name()][0]
                    install_script = this_output.get('script')
                    # # HACK: conda-build renames the actual test script from the recipe into
                    # #    run_test.* in the package.  This makes the test discovery code work.
                    # if test_script:
                    #     ext = os.path.splitext(test_script)[1]
                    #     test_script = 'run_test' + ext
                    build_inputs = []
                    for build_input in ('build.sh', 'bld.bat'):
                        if os.path.isfile(os.path.join(parent_recipe_path, build_input)):
                            build_inputs.append(build_input)
                    inputs = [install_script] + build_inputs
                    file_paths = [script for script in inputs if script]
                    file_paths = filter_files(file_paths, parent_recipe_path)
                else:
                    files = utils.rec_glob(self.path, "*")
                    file_paths = sorted([f.replace(self.path + os.sep, '') for f in files])
                    # exclude meta.yaml because the json dictionary captures its content
                    file_paths = [f for f in file_paths if not (f == 'meta.yaml' or
                                                                f.startswith('run_test') or
                                                                f == 'conda_build_config.yaml')]
                    file_paths = filter_files(file_paths, self.path)
        trim_empty_keys(composite)
        return composite, sorted(file_paths)

    def hash_dependencies(self):
        """With arbitrary pinning, we can't depend on the build string as done in
        build_string_from_metadata - there's just too much info.  Instead, we keep that as-is, to
        not be disruptive, but we add this extra hash, which is just a way of distinguishing files
        on disk.  The actual determination of dependencies is done in the repository metadata."""
        # save only the first HASH_LENGTH characters - should be more than enough, since these only
        #    need to be unique within one version
        # plus one is for the h - zero pad on the front, trim to match HASH_LENGTH
        recipe_input, file_paths = self.get_hash_contents()
        hash_ = hashlib.sha1(json.dumps(recipe_input, sort_keys=True).encode())
        recipe_path = (self.path or
                        self.meta.get('extra', {}).get('parent_recipe', {}).get('path'))
        if recipe_path:
            for recipe_file in file_paths:
                with open(os.path.join(recipe_path, recipe_file), 'rb') as f:
                    hash_.update(f.read())
        hash_ = 'h{0}'.format(hash_.hexdigest())[:self.config.hash_length + 1]
        return hash_

    def build_id(self):
        manual_build_string = re.search("\s*string:", self.extract_package_and_build_text())
        # default; build/string not set
        if not manual_build_string or re.findall('h\{\{\s*PKG_HASH\s*\}\}',
                                                 manual_build_string.string):
            out = build_string_from_metadata(self)
            if self.config.filename_hashing and self.final:
                if not re.findall('h[0-9a-f]{%s}' % self.config.hash_length, out):
                    ret = out.rsplit('_', 1)
                    try:
                        int(ret[0])
                        out = self.hash_dependencies() + '_' + str(ret[0])
                    except ValueError:
                        out = ret[0] + self.hash_dependencies()
                    if len(ret) > 1:
                        out = '_'.join([out] + ret[1:])
                else:
                    out = re.sub('h[0-9a-f]{%s}' % self.config.hash_length,
                                 self.hash_dependencies(),
                                 out)
        # user setting their own build string.  Don't modify it.
        else:
            out = self.get_value('build/string')
            check_bad_chrs(out, 'build/string')
        return out

    def dist(self):
        return '%s-%s-%s' % (self.name(), self.version(), self.build_id())

    def pkg_fn(self):
        return "%s.tar.bz2" % self.dist()

    def is_app(self):
        return bool(self.get_value('app/entry'))

    def app_meta(self):
        d = {'type': 'app'}
        if self.get_value('app/icon'):
            d['icon'] = '%s.png' % md5_file(join(
                self.path, self.get_value('app/icon')))

        for field, key in [('app/entry', 'app_entry'),
                           ('app/type', 'app_type'),
                           ('app/cli_opts', 'app_cli_opts'),
                           ('app/summary', 'summary'),
                           ('app/own_environment', 'app_own_environment')]:
            value = self.get_value(field)
            if value:
                d[key] = value
        return d

    def info_index(self):
        arch = 'noarch' if self.config.target_subdir == 'noarch' else self.config.host_arch
        d = dict(
            name=self.name(),
            version=self.version(),
            build=self.build_id(),
            build_number=self.build_number() if self.build_number() else 0,
            platform=self.config.platform if (self.config.platform != 'noarch' and
                                              arch != 'noarch') else None,
            arch=ARCH_MAP.get(arch, arch),
            subdir=self.config.target_subdir,
            depends=sorted(' '.join(ms.spec.split())
                             for ms in self.ms_depends()),
            timestamp=int(time.time() * 1000),
        )
        for key in ('license', 'license_family'):
            value = self.get_value('about/' + key)
            if value:
                d[key] = value

        preferred_env = self.get_value('build/preferred_env')
        if preferred_env:
            d['preferred_env'] = preferred_env

        # conda 4.4+ optional dependencies
        constrains = ensure_list(self.get_value('requirements/run_constrained'))
        # filter None values
        constrains = [v for v in constrains if v]
        if constrains:
            d['constrains'] = constrains

        if self.get_value('build/features'):
            d['features'] = ' '.join(self.get_value('build/features'))
        if self.get_value('build/track_features'):
            d['track_features'] = ' '.join(self.get_value('build/track_features'))
        if self.get_value('build/provides_features'):
            d['provides_features'] = self.get_value('build/provides_features')
        if self.get_value('build/requires_features'):
            d['requires_features'] = self.get_value('build/requires_features')
        if self.noarch:
            d['platform'] = d['arch'] = None
            d['subdir'] = 'noarch'
            # These are new-style noarch settings.  the self.noarch setting can be True in 2 ways:
            #    if noarch: True or if noarch_python: True.  This is disambiguation.
            build_noarch = self.get_value('build/noarch')
            if build_noarch:
                d['noarch'] = build_noarch
        if self.is_app():
            d.update(self.app_meta())
        return d

    def has_prefix_files(self):
        ret = ensure_list(self.get_value('build/has_prefix_files', []))
        if not isinstance(ret, list):
            raise RuntimeError('build/has_prefix_files should be a list of paths')
        if sys.platform == 'win32':
            if any('\\' in i for i in ret):
                raise RuntimeError("build/has_prefix_files paths must use / "
                                   "as the path delimiter on Windows")
        return expand_globs(ret, self.config.build_prefix)

    def ignore_prefix_files(self):
        ret = self.get_value('build/ignore_prefix_files', False)
        if type(ret) not in (list, bool):
            raise RuntimeError('build/ignore_prefix_files should be boolean or a list of paths '
                               '(optionally globs)')
        if sys.platform == 'win32':
            if type(ret) is list and any('\\' in i for i in ret):
                raise RuntimeError("build/ignore_prefix_files paths must use / "
                                   "as the path delimiter on Windows")
        return expand_globs(ret, self.config.build_prefix) if type(ret) is list else ret

    def always_include_files(self):
        files = ensure_list(self.get_value('build/always_include_files', []))
        if any('\\' in i for i in files):
            raise RuntimeError("build/always_include_files paths must use / "
                                "as the path delimiter on Windows")
        if on_win:
            files = [f.replace("/", "\\") for f in files]

        return expand_globs(files, self.config.build_prefix)

    def binary_relocation(self):
        ret = self.get_value('build/binary_relocation', True)
        if type(ret) not in (list, bool):
            raise RuntimeError('build/ignore_prefix_files should be boolean or a list of paths '
                               '(optionally globs)')
        if sys.platform == 'win32':
            if type(ret) is list and any('\\' in i for i in ret):
                raise RuntimeError("build/ignore_prefix_files paths must use / "
                                   "as the path delimiter on Windows")
        return expand_globs(ret, self.config.build_prefix) if type(ret) is list else ret

    def include_recipe(self):
        return self.get_value('build/include_recipe', True)

    def binary_has_prefix_files(self):
        ret = ensure_list(self.get_value('build/binary_has_prefix_files', []))
        if not isinstance(ret, list):
            raise RuntimeError('build/binary_has_prefix_files should be a list of paths')
        if sys.platform == 'win32':
            if any('\\' in i for i in ret):
                raise RuntimeError("build/binary_has_prefix_files paths must use / "
                                   "as the path delimiter on Windows")
        return expand_globs(ret, self.config.build_prefix)

    def skip(self):
        return self.get_value('build/skip', False)

    def _get_contents(self, permit_undefined_jinja, allow_no_other_outputs=False,
                      bypass_env_check=False):
        '''
        Get the contents of our [meta.yaml|conda.yaml] file.
        If jinja is installed, then the template.render function is called
        before standard conda macro processors.

        permit_undefined_jinja: If True, *any* use of undefined jinja variables will
                                evaluate to an emtpy string, without emitting an error.
        '''
        try:
            import jinja2
        except ImportError:
            print("There was an error importing jinja2.", file=sys.stderr)
            print("Please run `conda install jinja2` to enable jinja template support", file=sys.stderr)  # noqa
            with open(self.meta_path) as fd:
                return fd.read()

        from conda_build.jinja_context import context_processor, UndefinedNeverFail, FilteredLoader

        path, filename = os.path.split(self.meta_path)
        loaders = [  # search relative to '<conda_root>/Lib/site-packages/conda_build/templates'
                   jinja2.PackageLoader('conda_build'),
                   # search relative to RECIPE_DIR
                   jinja2.FileSystemLoader(path)
                   ]

        # search relative to current conda environment directory
        conda_env_path = os.environ.get('CONDA_DEFAULT_ENV')  # path to current conda environment
        if conda_env_path and os.path.isdir(conda_env_path):
            conda_env_path = os.path.abspath(conda_env_path)
            conda_env_path = conda_env_path.replace('\\', '/')  # need unix-style path
            env_loader = jinja2.FileSystemLoader(conda_env_path)
            loaders.append(jinja2.PrefixLoader({'$CONDA_DEFAULT_ENV': env_loader}))

        undefined_type = jinja2.StrictUndefined
        if permit_undefined_jinja:
            # The UndefinedNeverFail class keeps a global list of all undefined names
            # Clear any leftover names from the last parse.
            UndefinedNeverFail.all_undefined_names = []
            undefined_type = UndefinedNeverFail

        loader = FilteredLoader(jinja2.ChoiceLoader(loaders), config=self.config)
        env = jinja2.Environment(loader=loader, undefined=undefined_type)

        env.globals.update(ns_cfg(self.config))
        env.globals.update(context_processor(self, path, config=self.config,
                                             permit_undefined_jinja=permit_undefined_jinja,
                                             allow_no_other_outputs=allow_no_other_outputs,
                                             bypass_env_check=bypass_env_check))

        # Future goal here.  Not supporting jinja2 on replaced sections right now.

        # we write a temporary file, so that we can dynamically replace sections in the meta.yaml
        #     file on disk.  These replaced sections also need to have jinja2 filling in templates.
        # The really hard part here is that we need to operate on plain text, because we need to
        #     keep selectors and all that.

        try:
            template = env.get_or_select_template(filename)
            rendered = template.render(environment=env)

            if permit_undefined_jinja:
                self.undefined_jinja_vars = UndefinedNeverFail.all_undefined_names
            else:
                self.undefined_jinja_vars = []

        except jinja2.TemplateError as ex:
            if "'None' has not attribute" in str(ex):
                ex = "Failed to run jinja context function"
            sys.exit("Error: Failed to render jinja template in {}:\n{}"
                     .format(self.meta_path, str(ex)))
        return rendered

    def __unicode__(self):
        '''
        String representation of the MetaData.
        '''
        return text_type(self.__dict__)

    def __str__(self):
        if PY3:
            return self.__unicode__()
        else:
            return self.__unicode__().encode('utf-8')

    def __repr__(self):
        '''
        String representation of the MetaData.
        '''
        return self.__str__()

    @property
    def uses_setup_py_in_meta(self):
        meta_text = ''
        meta_path = (self.meta_path or
                     self.meta.get('extra', {}).get('parent_recipe', {}).get('path'))
        if meta_path:
            with open(self.meta_path, 'rb') as f:
                meta_text = UnicodeDammit(f.read()).unicode_markup
        return u"load_setup_py_data" in meta_text or u"load_setuptools" in meta_text

    @property
    def uses_regex_in_meta(self):
        meta_text = ""
        if self.meta_path:
            with open(self.meta_path, 'rb') as f:
                meta_text = UnicodeDammit(f.read()).unicode_markup
        return "load_file_regex" in meta_text

    @property
    def needs_source_for_render(self):
        return self.uses_vcs_in_meta or self.uses_setup_py_in_meta or self.uses_regex_in_meta

    @property
    def uses_jinja(self):
        if not self.meta_path:
            return False
        with open(self.meta_path, 'rb') as f:
            meta_text = UnicodeDammit(f.read()).unicode_markup
            matches = re.findall(r"{{.*}}", meta_text)
        return len(matches) > 0

    @property
    def uses_vcs_in_meta(self):
        """returns name of vcs used if recipe contains metadata associated with version control systems.
        If this metadata is present, a download/copy will be forced in parse_or_try_download.
        """
        vcs = None
        vcs_types = ["git", "svn", "hg"]
        # We would get here if we use Jinja2 templating, but specify source with path.
        if self.meta_path:
            with open(self.meta_path) as f:
                metayaml = f.read()
                for _vcs in vcs_types:
                    matches = re.findall(r"{}_[^\.\s\'\"]+".format(_vcs.upper()), metayaml)
                    if len(matches) > 0 and _vcs != self.meta['package']['name']:
                        if _vcs == "hg":
                            _vcs = "mercurial"
                        vcs = _vcs
                        break
        return vcs

    @property
    def uses_vcs_in_build(self):
        build_script = "bld.bat" if on_win else "build.sh"
        build_script = os.path.join(os.path.dirname(self.meta_path), build_script)
        for recipe_file in (build_script, self.meta_path):
            if os.path.isfile(recipe_file):
                vcs_types = ["git", "svn", "hg"]
                with open(recipe_file) as f:
                    build_script = f.read()
                    for vcs in vcs_types:
                        # commands are assumed to have 3 parts:
                        #   1. the vcs command, optionally with an exe extension
                        #   2. a subcommand - for example, "clone"
                        #   3. a target url or other argument
                        matches = re.findall(r"{}(?:\.exe)?(?:\s+\w+\s+[\w\/\.:@]+)".format(vcs),
                                            build_script, flags=re.IGNORECASE)
                        if len(matches) > 0 and vcs != self.meta['package']['name']:
                            if vcs == "hg":
                                vcs = "mercurial"
                            return vcs
        return None

    def get_recipe_text(self, extract_pattern=None):
        recipe_text = ""
        if self.meta_path:
            with open(self.meta_path) as f:
                recipe_text = f.read()
            if PY3 and hasattr(recipe_text, 'decode'):
                recipe_text = recipe_text.decode()
            if extract_pattern:
                match = re.search(extract_pattern, recipe_text, flags=re.MULTILINE | re.DOTALL)
                recipe_text = match.group(1) if match else ""
        recipe_text = select_lines(recipe_text, ns_cfg(self.config),
                                   variants_in_place=bool(self.config.variant))
        return recipe_text

    def extract_requirements_text(self):
        return self.get_recipe_text(r'(^requirements:.*?)(^test:|^extra:|^about:|^outputs:|\Z)')

    def extract_source_text(self):
        return self.get_recipe_text(
            r'(\s*source:.*?)(^build:|^requirements:|^test:|^extra:|^about:|^outputs:|\Z)')

    def extract_package_and_build_text(self):
        return self.get_recipe_text(r'(^.*?)(^requirements:|^test:|^extra:|^about:|^outputs:|\Z)')

    @property
    def numpy_xx(self):
        '''This is legacy syntax that we need to support for a while.  numpy x.x means
        "pin run as build" for numpy.  It was special-cased to only numpy.'''
        text = self.extract_requirements_text()
        uses_xx = bool(re.search(r'numpy\s*x\.x', text))
        if uses_xx:
            log = utils.get_logger(__name__)
            log.warn("Recipe at {path} uses numpy x.x.  This is deprecated as of conda-build 3.0, "
                     "and will be removed in conda-build 4.0.  Please consider using variants with "
                     "pin_run_as_build instead.  More info at "
                     "https://conda.io/docs/building/variants.html#customizing-compatibility"
                     .format(path=self.path))
        return uses_xx

    @property
    def uses_subpackage(self):
        outputs = self.get_section('outputs')
        in_reqs = False
        for out in outputs:
            if 'name' in out:
                name_re = re.compile(r"^{}(\s|\Z|$)".format(out['name']))
                in_reqs = any(name_re.match(req) for req in self.get_value('requirements/run'))
        subpackage_pin = False
        if not in_reqs and self.meta_path:
                data = self.extract_requirements_text()
                if data:
                    subpackage_pin = re.search("{{\s*pin_subpackage\(.*\)\s*}}", data)
        return in_reqs or bool(subpackage_pin)

    @property
    def uses_new_style_compiler_activation(self):
        text = self.extract_requirements_text()
        return bool(re.search(r'\{\{\s*compiler\(.*\)\s*\}\}', text))

    def validate_features(self):
        if any('-' in feature for feature in ensure_list(self.get_value('build/features'))):
            raise ValueError("- is a disallowed character in features.  Please change this "
                             "character in your recipe.")

    def copy(self):
        new = copy.copy(self)
        new.config = self.config.copy()
        new.meta = copy.deepcopy(self.meta)
        return new

    @property
    def noarch(self):
        return self.get_value('build/noarch')

    @noarch.setter
    def noarch(self, value):
        build = self.meta.get('build', {})
        build['noarch'] = value
        self.meta['build'] = build
        if not self.noarch_python and not value:
            self.config.reset_platform()
        elif value:
            self.config.host_platform = 'noarch'

    @property
    def noarch_python(self):
        return self.get_value('build/noarch_python')

    @noarch_python.setter
    def noarch_python(self, value):
        build = self.meta.get('build', {})
        build['noarch_python'] = value
        self.meta['build'] = build
        if not self.noarch and not value:
            self.config.reset_platform()
        elif value:
            self.config.host_platform = 'noarch'

    @property
    def variant_in_source(self):
        variant = self.config.variant
        self.config.variant = {}
        self.parse_again(permit_undefined_jinja=True, allow_no_other_outputs=True,
                         bypass_env_check=True)
        vars_in_recipe = set(self.undefined_jinja_vars)
        self.config.variant = variant

        for key in (vars_in_recipe & set(variant.keys())):
            # We use this variant in the top-level recipe.
            # constrain the stored variants to only this version in the output
            #     variant mapping
            if re.search(r"\s*\{\{\s*%s\s*(?:.*?)?\}\}" % key, self.extract_source_text()):
                return True
        return False

    @property
    def pin_depends(self):
        return self.get_value('build/pin_depends', '').lower()

    @property
    def source_provided(self):
        return (not bool(self.meta.get('source')) or
                (os.path.isdir(self.config.work_dir) and len(os.listdir(self.config.work_dir)) > 0))

    def reconcile_metadata_with_output_dict(self, output_metadata, output_dict):
        output_metadata.meta['package']['name'] = output_dict.get('name', self.name())

        # make sure that subpackages do not duplicate tests from top-level recipe
        test = output_metadata.meta.get('test', {})
        if output_dict.get('name') != self.name():
            if 'commands' in test:
                del test['commands']
            if 'imports' in test:
                del test['imports']

        # make sure that subpackages do not duplicate top-level entry-points
        build = output_metadata.meta.get('build', {})
        if 'entry_points' in output_dict:
            build['entry_points'] = output_dict['entry_points']
        elif 'entry_points' in build:
            del build['entry_points']
        output_metadata.meta['build'] = build

        # reset these so that reparsing does not reset the metadata name
        output_metadata.path = ""
        output_metadata.meta_path = ""

    def get_output_metadata(self, output):
        if output.get('target'):
            self.config.target_subdir = output['target']
        output_metadata = self.copy()
        if self.name() != output.get('name'):
            self.reconcile_metadata_with_output_dict(output_metadata, output)

        if 'type' in output and output['type'] != 'conda':
            name = output.get('name', self.name()) + '_' + output['type']
            output_metadata.meta['package']['name'] = name

        output_reqs = output.get('requirements', {})
        if hasattr(output_reqs, 'keys'):
            build_reqs = output_reqs.get('build', [])
            host_reqs = output_reqs.get('host', [])
            run_reqs = output_reqs.get('run', [])
            constrain_reqs = output_reqs.get('run_constrained', [])
            # pass through any other unrecognized req types
            other_reqs = {k: v for k, v in output_reqs.items() if k not in
                          ('build', 'host', 'run', 'run_constrained')}
        else:
            output_reqs = ensure_list(output_reqs)
            build_reqs = output_reqs
            host_reqs = []
            run_reqs = output_reqs
            constrain_reqs = []
            other_reqs = {}

        if 'name' in output:
            # since we are copying reqs from the top-level package, which
            #   can depend on subpackages, make sure that we filter out
            #   subpackages so that they don't depend on themselves
            subpackage_pattern = re.compile(r'(?:^{}(?:\s|$|\Z))'.format(output['name']))
            build_reqs = [req for req in build_reqs if not subpackage_pattern.match(req)]
            host_reqs = [req for req in host_reqs if not subpackage_pattern.match(req)]
            run_reqs = [req for req in run_reqs if not subpackage_pattern.match(req)]

        requirements = {'build': build_reqs, 'host': host_reqs, 'run': run_reqs}
        if constrain_reqs:
            requirements['run_constrained'] = constrain_reqs
        requirements.update(other_reqs)
        output_metadata.meta['requirements'] = requirements
        for env in ('build', 'host'):
            insert_variant_versions(output_metadata, env)
        output_metadata.meta['package']['version'] = output.get('version') or self.version()
        extra = self.meta.get('extra', {})
        if self.name() == output.get('name') and 'requirements' not in output:
            output['requirements'] = requirements
        output_metadata.meta['extra'] = extra
        output_metadata.final = False
        if self.name() != output_metadata.name():
            extra = self.meta.get('extra', {})
            extra['parent_recipe'] = {'path': self.path, 'name': self.name(),
                                      'version': self.version()}
            output_metadata.meta['extra'] = extra
        output_metadata.noarch = output.get('noarch', False)
        output_metadata.noarch_python = output.get('noarch_python', False)
        # primarily for tests - make sure that we keep the platform consistent (setting noarch
        #      would reset it)
        if (not (output_metadata.noarch or output_metadata.noarch_python) and
                self.config.platform != output_metadata.config.platform):
            output_metadata.config.platform = self.config.platform

        build = output_metadata.meta.get('build', {})
        # legacy (conda build 2.1.x - 3.0.25)
        if 'number' in output:
            build['number'] = output['number']
        if 'string' in output:
            build['string'] = output['string']
        if 'run_exports' in output and output['run_exports']:
            build['run_exports'] = output['run_exports']
        if 'track_features' in output and output['track_features']:
            build['track_features'] = output['track_features']
        if 'features' in output and output['features']:
            build['features'] = output['features']
        # 3.0.26+ - just pass through the whole build section from the output.
        #    It clobbers everything else.
        if 'build' in output:
            build = output['build']
        output_metadata.meta['build'] = build
        if 'test' in output:
            output_metadata.meta['test'] = output['test']
        if 'about' in output:
            output_metadata.meta['about'] = output['about']

        return output_metadata

    def get_output_metadata_set(self, permit_undefined_jinja=False,
                                permit_unsatisfiable_variants=False):
        from conda_build.source import provide
        out_metadata_map = {}

        if self.final:
            outputs = get_output_dicts_from_metadata(self)[0]
            output_tuples = [(outputs, self)]
        else:
            for variant in (self.config.variants if hasattr(self.config, 'variants')
                            else [self.config.variant]):
                om = self.copy()
                om.config.variant = variant
                if om.needs_source_for_render and om.variant_in_source:
                    om.parse_again()
                    utils.rm_rf(om.config.work_dir)
                    provide(om)
                    om.parse_again()
                om.parse_until_resolved(allow_no_other_outputs=True, bypass_env_check=True)
                outputs = get_output_dicts_from_metadata(om)

                try:
                    for out in outputs:
                        out_metadata_map[HashableDict(out)] = om.get_output_metadata(out)
                except SystemExit:
                    if not permit_undefined_jinja:
                        raise
                    out_metadata_map = {}

            assert out_metadata_map, ("Error: output metadata set is empty.  Please file an issue"
                    " on the conda-build tracker at https://github.com/conda/conda-build/issues")

            # format here is {output_dict: metadata_object}
            render_order = toposort(out_metadata_map, phase='run')
            check_circular_dependencies(render_order)

            conda_packages = OrderedDict()
            non_conda_packages = []
            for output_d, m in render_order.items():
                if not output_d.get('type') or output_d['type'] == 'conda':
                    conda_packages[m.name(), HashableDict(m.config.variant)] = (output_d, m)
                elif output_d.get('type') == 'wheel':
                    if (not output_d.get('requirements', {}).get('build') or
                            not any('wheel' in req for req in output_d['requirements']['build'])):
                        build_reqs = output_d.get('requirements', {}).get('build', [])
                        build_reqs.extend(['wheel', 'python {}'.format(m.config.variant['python'])])
                        output_d['requirements'] = output_d.get('requirements', {})
                        output_d['requirements']['build'] = build_reqs
                        m.meta['requirements'] = m.meta.get('requirements', {})
                        m.meta['requirements']['build'] = build_reqs
                    non_conda_packages.append((output_d, m))
                else:
                    # for wheels and other non-conda packages, just append them at the end.
                    #    no deduplication with hashes currently.
                    # hard part about including any part of output_d
                    #    outside of this func is that it is harder to
                    #    obtain an exact match elsewhere
                    non_conda_packages.append((output_d, m))

            # early stages don't need to do the finalization.  Skip it until the later stages
            #     when we need it.
            if not permit_undefined_jinja:
                # The loop above gives us enough info to determine the build order.  After that,
                #    we can "finalize" the metadata (fill in version pins) for the build metadata.
                #    This does not, however, account for circular dependencies
                #    where a runtime exact dependency pin on a downstream build
                #    subpackage in an upstream subpackage changes the upstream's
                #    hash, which then changes the downstream package's hash, which
                #    then recurses infinitely

                # In general, given upstream a and downstream b, b can depend on a
                # exactly, but a can only have version constraints on b at run or
                # run_exports, not exact=True

                # 3 passes here:
                #    1. fill in fully-resolved build-time dependencies
                #    2. fill in fully-resolved run-time dependencies.  Note that circular deps
                #       are allowed, but you can't have exact=True for circular run-time deps
                #    3. finally, everything should be filled in and done.
                conda_packages = finalize_outputs_pass(self, conda_packages, pass_no=0,
                                    permit_unsatisfiable_variants=permit_unsatisfiable_variants)

                # Sanity check: if any exact pins of any subpackages, make sure that they match
                ensure_matching_hashes(conda_packages)
            # We arbitrarily mark all output metadata as final, regardless of if it truly is or not.
            #    This is done to add sane hashes to unfinalizable packages, so that they are
            #    differentiable from one another.  This is mostly a test concern than an actual one,
            #    as any "final" recipe returned here will still barf if anyone tries to actually
            #    build it.
            final_conda_packages = []
            for (out_d, m) in conda_packages.values():
                m.final = True
                final_conda_packages.append((out_d, m))
            output_tuples = final_conda_packages + non_conda_packages
        return output_tuples

    def get_loop_vars(self):
        _variants = (self.config.input_variants if hasattr(self.config, 'input_variants') else
                    self.config.variants)
        return variants.get_loop_vars(_variants)

    def get_used_loop_vars(self):
        used_variables = set()
        # recipe text is the best, because variables can be used anywhere in it.
        #   we promise to detect anything in meta.yaml, but not elsewhere.
        recipe_text = self.get_recipe_text()
        # fall back to requirements when recipe text is unavailable
        if not recipe_text:
            requirements = (self.get_value('requirements/build') +
                    self.get_value('requirements/run') +
                    self.get_value('requirements/host'))
            recipe_text = '- ' + "\n- ".join(requirements)
        for v in self.get_loop_vars():
            variant_regex = r"(\s*\{\{\s*%s\s*(?:.*?)?\}\})" % v
            requirement_regex = r"(\-\s+%s(?:\s+|$))" % v
            all_res = '|'.join((variant_regex, requirement_regex))
            compiler_match = re.match(r'(.*?)_compiler$', v)
            if compiler_match:
                compiler_regex = (
                    r"(\s*\{\{\s*compiler\([\'\"]%s[\"\'].*\)\s*\}\})" % compiler_match.group(1))
                all_res = '|'.join((all_res, compiler_regex))
            if re.search(all_res, recipe_text, flags=re.MULTILINE | re.DOTALL):
                used_variables.add(v)
        return used_variables

    def get_variants_as_dict_of_lists(self):
        return variants.list_of_dicts_to_dict_of_lists(self.config.variants)

    def clean(self):
        """This ensures that clean is called with the correct build id"""
        self.config.clean()

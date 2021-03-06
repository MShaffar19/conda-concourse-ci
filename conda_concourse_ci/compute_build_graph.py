#!/usr/bin/env python
from __future__ import division, print_function

import logging
import os
import re
import subprocess

from conda_build import api, conda_interface
from conda_build.build import is_package_built
from conda_build.metadata import MetaData, find_recipe

import networkx as nx

import pkg_resources

from .utils import HashableDict, ensure_list


log = logging.getLogger(__file__)
CONDA_BUILD_CACHE = os.environ.get("CONDA_BUILD_CACHE")
hash_length = api.Config().hash_length


def package_key(metadata, worker_label, run='build'):
    # get the build string from whatever conda-build makes of the configuration

    used_loop_vars = metadata.get_used_loop_vars()
    build_vars = '-'.join([k + '_' + str(metadata.config.variant[k]) for k in used_loop_vars
                          if k != 'target_platform'])
    # kind of a special case.  Target platform determines a lot of output behavior, but may not be
    #    explicitly listed in the recipe.
    tp = metadata.config.variant.get('target_platform')
    if tp and tp != metadata.config.subdir:
        build_vars += '-target_' + tp
    key = [metadata.name(), metadata.version()]
    if build_vars:
        key.append(build_vars)
    key.extend(['on', worker_label])
    key = "-".join(key)
    if run == 'test':
        key = '-'.join(('c3itest', key))
    key = key.replace(' ', '_')
    return key


def _git_changed_files(git_rev, stop_rev=None, git_root=''):
    if not git_root:
        git_root = os.getcwd()
    if stop_rev:
        git_rev = "{0}..{1}".format(git_rev, stop_rev)
    output = subprocess.check_output(['git', 'diff-tree', '--no-commit-id',
                                      '--name-only', '-r', git_rev],
                                     cwd=git_root)
    files = output.decode().splitlines()
    return files


def _get_base_folders(base_dir, changed_files):
    recipe_dirs = []
    for f in changed_files:
        # only consider files that come from folders
        if '/' in f:
            f = f.split('/')[0]
        try:
            find_recipe(os.path.join(base_dir, f))
            recipe_dirs.append(f)
        except IOError:
            pass
    return recipe_dirs


def git_changed_submodules(git_rev='HEAD@{1}', stop_rev=None, git_root='.'):
    if stop_rev is not None:
        git_rev = "{0}..{1}".format(git_rev, stop_rev)
    diff_script = pkg_resources.resource_filename('conda_concourse_ci', 'diff-script.sh')

    diff = subprocess.check_output(['bash', diff_script, git_rev],
                                    cwd=git_root, universal_newlines=True)

    submodule_changed_files = [line.split() for line in diff.splitlines()]

    submodules_with_recipe_changes = []
    for submodule in submodule_changed_files:
        for file in submodule:
            if 'recipe/' in file and submodule[0] not in submodules_with_recipe_changes:
                submodules_with_recipe_changes.append(submodule[0])

    return submodules_with_recipe_changes


def git_new_submodules(git_rev='HEAD@{1}', stop_rev=None, git_root='.'):
    if stop_rev is not None:
        git_rev = "{0}..{1}".format(git_rev, stop_rev)

    new_submodule_script = pkg_resources.resource_filename('conda_concourse_ci',
                                                           'new-submodule-script.sh')

    diff = subprocess.check_output(['bash', new_submodule_script, git_rev],
                                    cwd=git_root, universal_newlines=True)

    return diff.splitlines()


def git_renamed_folders(git_rev='HEAD@{1}', stop_rev=None, git_root='.'):
    if stop_rev is not None:
        git_rev = "{0}..{1}".format(git_rev, stop_rev)

    rename_script = pkg_resources.resource_filename('conda_concourse_ci',
                                                    'rename-script.sh')

    renamed_files = subprocess.check_output(['bash', rename_script], cwd=git_root,
                                             universal_newlines=True).splitlines()

    return renamed_files


def git_changed_recipes(git_rev='HEAD@{1}', stop_rev=None, git_root='.'):
    """
    Get the list of files changed in a git revision and return a list of
    package directories that have been modified.

    git_rev: if stop_rev is not provided, this represents the changes
             introduced by the given git rev.  It is equivalent to
             git_rev=SOME_REV@{1} and stop_rev=SOME_REV

    stop_rev: when provided, this is the end of a range of revisions to
             consider.  git_rev becomes the start revision.  Note that the
             start revision is *one before* the actual start of examining
             commits for changes.  In other words:

             git_rev=SOME_REV@{1} and stop_rev=SOME_REV   => only SOME_REV
             git_rev=SOME_REV@{2} and stop_rev=SOME_REV   => two commits, SOME_REV and the
                                                             one before it
    """
    changed_files = _git_changed_files(git_rev, stop_rev, git_root)
    recipe_dirs = _get_base_folders(git_root, changed_files)
    changed_submodules = git_changed_submodules(git_rev, stop_rev, git_root)
    new_submodules = git_new_submodules(git_rev, stop_rev, git_root)
    renamed_folders = git_renamed_folders(git_rev, stop_rev, git_root)
    return recipe_dirs + changed_submodules + new_submodules + renamed_folders


def _deps_to_version_dict(deps):
    d = {}
    for x in deps:
        x = x.strip().split()
        if len(x) == 3:
            d[x[0]] = (x[1], x[2])
        elif len(x) == 2:
            d[x[0]] = (x[1], 'any')
        else:
            d[x[0]] = ('any', 'any')
    return d


def get_build_deps(meta):
    build_reqs = meta.get_value('requirements/build')
    if not build_reqs:
        build_reqs = []
    return _deps_to_version_dict(build_reqs)


def get_run_test_deps(meta):
    run_reqs = meta.get_value('requirements/run')
    if not run_reqs:
        run_reqs = []
    test_reqs = meta.get_value('test/requires')
    if not test_reqs:
        test_reqs = []
    return _deps_to_version_dict(run_reqs + test_reqs)


_rendered_recipes = {}


@conda_interface.memoized
def _get_or_render_metadata(meta_file_or_recipe_dir, worker, finalize, config=None):
    global _rendered_recipes
    label = worker['label']
    platform = worker['platform']
    arch = str(worker['arch'])
    if (meta_file_or_recipe_dir, label, platform, arch) not in _rendered_recipes:
        print("rendering {0} for {1}".format(meta_file_or_recipe_dir, worker['label']))
        _rendered_recipes[(meta_file_or_recipe_dir, label, platform, arch)] = \
                            api.render(meta_file_or_recipe_dir, platform=platform, arch=arch,
                                       verbose=False, permit_undefined_jinja=True,
                                       bypass_env_check=True, config=config, finalize=finalize)
    return _rendered_recipes[(meta_file_or_recipe_dir, label, platform, arch)]


def add_recipe_to_graph(recipe_dir, graph, run, worker, conda_resolve,
                        recipes_dir=None, config=None, finalize=False):
    try:
        rendered = _get_or_render_metadata(recipe_dir, worker, config=config, finalize=finalize)
    except (IOError, SystemExit, RuntimeError) as e:
        log.warn('invalid recipe dir or other recipe issue: %s - skipping.  Error was %s',
                 recipe_dir, e)
        return None

    name = None
    for (metadata, _, _) in rendered:
        if (is_package_built(metadata, 'host', include_local=False) and config.skip_existing or
                metadata.skip()):
            continue

        name = package_key(metadata, worker['label'], run)
        noarch_pkg = metadata.noarch in ['python', 'generic', True]
        if noarch_pkg:
            # noarch python packages do not have a Python variant
            metadata.config.variant.pop('python', None)

        if name not in graph.nodes():
            graph.add_node(name, meta=metadata, worker=worker, noarch_pkg=noarch_pkg)
            add_dependency_nodes_and_edges(name, graph, run, worker, conda_resolve,
                                        recipes_dir=recipes_dir, finalize=finalize)

        # # add the test equivalent at the same time.  This is so that expanding can find it.
        # if run == 'build':
        #     add_recipe_to_graph(recipe_dir, graph, 'test', worker, conda_resolve,
        #                         recipes_dir=recipes_dir)
        #     test_key = package_key(metadata, worker['label'])
        #     graph.add_edge(test_key, name)
        #     upload_key = package_key(metadata, worker['label'])
        #     graph.add_node(upload_key, meta=metadata, worker=worker)
        #     graph.add_edge(upload_key, test_key)

    return name


def match_peer_job(target_matchspec, other_m, this_m=None):
    """target_matchspec comes from the recipe.  target_variant is the variant from the recipe whose
    deps we are matching.  m is the peer job, which must satisfy conda and also have matching keys
    for any keys that are shared between target_variant and m.config.variant"""
    match_dict = {'name': other_m.name(),
                'version': other_m.version(),
                'build': _fix_any(other_m.build_id(), other_m.config), }
    if conda_interface.conda_43:
        match_dict = conda_interface.Dist(name=match_dict['name'],
                                            dist_name='-'.join((match_dict['name'],
                                                                match_dict['version'],
                                                                match_dict['build'])),
                                            version=match_dict['version'],
                                            build_string=match_dict['build'],
                                            build_number=int(other_m.build_number() or 0),
                                            channel=None)
    matchspec_matches = target_matchspec.match(match_dict)

    variant_matches = True
    if this_m:
        other_m_used_vars = other_m.get_used_loop_vars()
        for v in this_m.get_used_loop_vars():
            if v in other_m_used_vars:
                variant_matches &= this_m.config.variant[v] == other_m.config.variant[v]
    return matchspec_matches and variant_matches


def add_intradependencies(graph):
    """ensure that downstream packages wait for upstream build/test (not use existing
    available packages)"""
    for node in graph.nodes():
        if 'meta' not in graph.nodes[node]:
            continue
        # get build dependencies
        m = graph.nodes[node]['meta']

        if hasattr(m, 'other_outputs'):
            internal_deps = tuple(i[0] for i in m.other_outputs)
        else:
            internal_deps = ()
        # this is pretty hard. Realistically, we would want to know
        # what the build and host platforms are on the build machine.
        # However, all we know right now is what machine we're actually
        # on (the one calculating the graph).
        deps = set(m.ms_depends('build') + m.ms_depends('host') + m.ms_depends('run') +
                   [conda_interface.MatchSpec(dep) for dep in
                    ensure_list((m.meta.get('test') or {}).get('requires'))])

        for dep in deps:
            # Ignore all dependecies that are outputs of the current recipe.
            # These may not always match because of version differents but
            # without this recipe with outputs which depend on each other
            # cannot be submitted
            if dep.name in internal_deps:
                continue
            name_matches = (n for n in graph.nodes() if graph.nodes[n]['meta'].name() == dep.name)
            for matching_node in name_matches:
                # are any of these build dependencies also nodes in our graph?
                match_meta = graph.nodes[matching_node]['meta']
                if (match_peer_job(conda_interface.MatchSpec(dep), match_meta, m) and
                         (node, matching_node) not in graph.edges()):
                    # inside if statement because getting used vars is expensive
                    shared_vars = set(match_meta.get_used_vars()) & set(m.get_used_vars())
                    # all vars in variant that they both use must line up
                    if all(match_meta.config.variant[v] == m.config.variant[v]
                            for v in shared_vars):
                        # add edges if they don't already exist
                        graph.add_edge(node, matching_node)


def collapse_subpackage_nodes(graph):
    """Collapse all subpackage nodes into their parent recipe node

    We get one node per output, but a given recipe can have multiple outputs.  It's important
    for dependency ordering in the graph that the outputs exist independently, but once those
    dependencies are established, we need to collapse subpackages down to a single job for the
    top-level recipe."""
    # group nodes by their recipe path first, then within those groups by their variant
    node_groups = {}

    for node in graph.nodes():
        if 'meta' in graph.nodes[node]:
            meta = graph.nodes[node]['meta']
            meta_path = meta.meta_path or meta.meta['extra']['parent_recipe']['path']
            master = False

            master_meta = MetaData(meta_path, config=meta.config)
            if master_meta.name() == meta.name():
                master = True
            group = node_groups.get(meta_path, {})
            subgroup = group.get(HashableDict(meta.config.variant), {})
            if master:
                if 'master' in subgroup:
                    print(f'tried to set {node} as master but {subgroup.get("master")} already is master.')
                    continue
                    raise ValueError("tried to set more than one node in a group as master")
                subgroup['master'] = node
            else:
                sps = subgroup.get('subpackages', [])
                sps.append(node)
                subgroup['subpackages'] = sps
            group[HashableDict(meta.config.variant)] = subgroup
            node_groups[meta_path] = group

    for recipe_path, group in node_groups.items():
        for variant, subgroup in group.items():
            # if no node is the top-level recipe (only outputs, no top-level output), need to obtain
            #     package/name from recipe given by common recipe path.
            subpackages = subgroup.get('subpackages')
            if 'master' not in subgroup:
                sp0 = graph.nodes[subpackages[0]]
                master_meta = MetaData(recipe_path, config=sp0['meta'].config)
                worker = sp0['worker']
                master_key = package_key(master_meta, worker['label'])
                graph.add_node(master_key, meta=master_meta, worker=worker)
                master = graph.nodes[master_key]
            else:
                master = subgroup['master']
                master_key = package_key(graph.nodes[master]['meta'],
                                         graph.nodes[master]['worker']['label'])
            # fold in dependencies for all of the other subpackages within a group.  This is just
            #     the intersection of the edges between all nodes.  Store this on the "master" node.
            if subpackages:
                # reassign any external dependencies on our subpackages to the top-level package
                for edge in [edge for edge in graph.edges() if edge[1] in subpackages]:
                    new_edge = edge[0], master_key
                    if new_edge not in graph.edges():
                        graph.add_edge(*new_edge)
                    graph.remove_edge(*edge)

                # reassign our subpackages' deps to the top-level package
                for edge in [edge for edge in graph.edges() if edge[0] in subpackages]:
                    new_edge = master_key, edge[1]
                    if new_edge not in graph.edges():
                        graph.add_edge(*new_edge)
                    graph.remove_edge(*edge)

                # remove nodes that have been folded into master nodes
                for subnode in subpackages:
                    graph.remove_node(subnode)

    # the reassignment can end up with a top-level package depending on itself.  Clean it up.
    to_remove = set()
    for edge in graph.edges():
        if graph.nodes[edge[0]]['meta'].name() == graph.nodes[edge[1]]['meta'].name():
            to_remove.add(edge)

    for edge in to_remove:
        graph.remove_edge(*edge)


def _write_recipe_log(path):
    if not os.path.exists(os.path.join(path, "meta.yaml")):
        path = os.path.join(path, "recipe")
    try:
        output = subprocess.check_output(['git', 'log'], cwd=path)
        with open(os.path.join(path, "recipe_log.txt"), "wb") as f:
            f.write(output)
    except subprocess.CalledProcessError as e:
        log.warn("Unable to produce recipe git log for %s. Error was: %s",
                 path, e)
        pass
    except FileNotFoundError as e:
        log.warn(f"File {path} does not exist. Error was {e}. Skipping.")
        pass


def construct_graph(recipes_dir, worker, run, conda_resolve, folders=(),
                    git_rev=None, stop_rev=None, matrix_base_dir=None,
                    config=None, finalize=False):
    '''
    Construct a directed graph of dependencies from a directory of recipes

    run: whether to use build or run/test requirements for the graph.  Avoids cycles.
          values: 'build' or 'test'.  Actually, only 'build' matters - otherwise, it's
                   run/test for any other value.
    '''
    matrix_base_dir = matrix_base_dir or recipes_dir
    if not os.path.isabs(recipes_dir):
        recipes_dir = os.path.normpath(os.path.join(os.getcwd(), recipes_dir))
    assert os.path.isdir(recipes_dir)

    if not folders:
        if not git_rev:
            git_rev = 'HEAD'

        folders = git_changed_recipes(git_rev, stop_rev=stop_rev,
                                      git_root=recipes_dir)

    graph = nx.DiGraph()
    print('starting to render the recipes')
    folders_len = len(folders)
    count = 0
    print(f'need to render {folders_len} folders')
    for folder in folders:
        recipe_dir = os.path.join(recipes_dir, folder)

        # update the recipe log.  Conda-build will find this and include it with the package.
        _write_recipe_log(recipe_dir)

        if not os.path.isdir(recipe_dir):
            raise ValueError("Specified folder {} does not exist".format(recipe_dir))
        add_recipe_to_graph(recipe_dir, graph, run, worker, conda_resolve,
                            recipes_dir, config=config, finalize=finalize)
        count += 1
        print(f'rendered {count} out of {folders_len} folders')
    print('rendered all folders')
    print('adding intradependencies')
    add_intradependencies(graph)
    print('successfully added intradependencies!')
    print('collapsing subpackage nodes')
    collapse_subpackage_nodes(graph)
    print('successfully collapsed subpackage nodes!')
    return graph


def _fix_any(value, config):
    value = re.sub('any(?:h[0-9a-f]{%d})?' % config.hash_length, '', value)
    return value


@conda_interface.memoized
def _installable(name, version, build_string, config, conda_resolve):
    """Can Conda install the package we need?"""
    ms = conda_interface.MatchSpec(" ".join([name, _fix_any(version, config),
                                             _fix_any(build_string, config)]))
    installable = conda_resolve.find_matches(ms)
    if not installable:
        log.warn("Dependency {name}, version {ver} is not installable from your "
                 "channels: {channels} with subdir {subdir}.  Seeing if we can build it..."
                 .format(name=name, ver=version, channels=config.channel_urls,
                         subdir=config.host_subdir))
    return installable


def _buildable(name, version, recipes_dir, worker, config, finalize):
    """Does the recipe that we have available produce the package we need?"""
    possible_dirs = os.listdir(recipes_dir)
    packagename_re = re.compile(r'%s(?:\-[0-9]+[\.0-9\_\-a-zA-Z]*)?$' % name)
    likely_dirs = (dirname for dirname in possible_dirs if
                    (os.path.isdir(os.path.join(recipes_dir, dirname)) and
                    packagename_re.match(dirname)))
    metadata_tuples = [m for path in likely_dirs
                        for (m, _, _) in _get_or_render_metadata(os.path.join(recipes_dir,
                                                                 path), worker, finalize=finalize)]

    # this is our target match
    ms = conda_interface.MatchSpec(" ".join([name, _fix_any(version, config)]))
    available = False
    for m in metadata_tuples:
        available = match_peer_job(ms, m)
        if available:
            break
    return m.meta_path if available else False


def add_dependency_nodes_and_edges(node, graph, run, worker, conda_resolve, recipes_dir=None,
                                   finalize=False):
    '''add build nodes for any upstream deps that are not yet installable

    changes graph in place.
    '''
    metadata = graph.nodes()[node]['meta']
    # for plain test runs, ignore build reqs.
    deps = get_run_test_deps(metadata)
    recipes_dir = recipes_dir or os.getcwd()

    # cross: need to distinguish between build_subdir (build reqs) and host_subdir
    if run == 'build':
        deps.update(get_build_deps(metadata))

    for dep, (version, build_str) in deps.items():
        # we don't need worker info in _installable because it is already part of conda_resolve
        if not _installable(dep, version, build_str, metadata.config, conda_resolve):
            recipe_dir = _buildable(dep, version, recipes_dir, worker, metadata.config,
                                    finalize=finalize)
            if not recipe_dir:
                continue
                # raise ValueError("Dependency {} is not installable, and recipe (if "
                #                  " available) can't produce desired version ({})."
                #                  .format(dep, version))
            dep_name = add_recipe_to_graph(recipe_dir, graph, 'build', worker,
                                            conda_resolve, recipes_dir, finalize=finalize)
            if not dep_name:
                raise ValueError("Tried to build recipe {0} as dependency, which is skipped "
                                 "in meta.yaml".format(recipe_dir))
            graph.add_edge(node, dep_name)


def expand_run_upstream(graph, conda_resolve, worker, run, steps=0, max_downstream=5,
                        recipes_dir=None, matrix_base_dir=None):
    pass


def expand_run(graph, config, conda_resolve, worker, run, steps=0, max_downstream=5,
               recipes_dir=None, matrix_base_dir=None, finalize=False):
    """Apply the build label to any nodes that need (re)building or testing.

    "need rebuilding" means both packages that our target package depends on,
    but are not yet built, as well as packages that depend on our target
    package. For the latter, you can specify how many dependencies deep (steps)
    to follow that chain, since it can be quite large.

    If steps is -1, all downstream dependencies are rebuilt or retested
    """
    downstream = 0
    initial_nodes = len(graph.nodes())

    # for build, we get test automatically.  Give people the max_downstream in terms
    #   of packages, not tasks
    # if run == 'build':
    #     max_downstream *= 2

    def expand_step(task_graph, full_graph, downstream):
        nodes = list(task_graph.nodes())
        for node in nodes:
            for predecessor in full_graph.predecessors(node):
                if max_downstream < 0 or (downstream - initial_nodes) < max_downstream:
                    add_recipe_to_graph(
                        os.path.dirname(full_graph.nodes[predecessor]['meta'].meta_path),
                        task_graph, config=config, run=run, worker=worker,
                        conda_resolve=conda_resolve,
                        recipes_dir=recipes_dir, finalize=finalize)
                    downstream += 1
        return len(task_graph.nodes())

    # starting from our initial collection of dirty nodes, trace the tree down to packages
    #   that depend on the dirty nodes.  These packages may need to be rebuilt, or perhaps
    #   just tested.  The 'run' argument determines which.

    if steps != 0:
        if not recipes_dir:
            raise ValueError("recipes_dir is necessary if steps != 0.  "
                             "Please pass it as an argument.")
        # here we need to fully populate a graph that has the right build or run/test deps.
        #    We don't create this elsewhere because it is unnecessary and costly.

        # get all immediate subdirectories
        other_top_dirs = [d for d in os.listdir(recipes_dir)
                        if os.path.isdir(os.path.join(recipes_dir, d)) and
                        not d.startswith('.')]
        recipe_dirs = []
        for recipe_dir in other_top_dirs:
            try:
                find_recipe(os.path.join(recipes_dir, recipe_dir))
                recipe_dirs.append(recipe_dir)
            except IOError:
                pass

        # constructing the graph for build will automatically also include the test deps
        full_graph = construct_graph(recipes_dir, worker, 'build', folders=recipe_dirs,
                                     matrix_base_dir=matrix_base_dir, conda_resolve=conda_resolve)

        if steps >= 0:
            for step in range(steps):
                downstream = expand_step(graph, full_graph, downstream)
        else:
            while True:
                nodes = list(graph.nodes())
                downstream = expand_step(graph, full_graph, downstream)
                if nodes == list(graph.nodes()):
                    break


def order_build(graph):
    '''
    Assumes that packages are in graph.
    Builds a temporary graph of relevant nodes and returns it topological sort.

    Relevant nodes selected in a breadth first traversal sourced at each pkg
    in packages.
    '''
    reorder_cyclical_test_dependencies(graph)
    try:
        order = list(nx.topological_sort(graph))
        order.reverse()
    except nx.exception.NetworkXUnfeasible:
        raise ValueError("Cycles detected in graph: %s", nx.find_cycle(graph))

    return order


def reorder_cyclical_test_dependencies(graph):
    """By default, we make things that depend on earlier outputs for build wait for tests of
    the earlier thing to pass.  However, circular dependencies spread across run/test and
    build/host can make this approach incorrect. For example:

    A <-- B  : B depends on A at build time
    B <-- A  : A depends on B at run time.  We can build A before B, but we cannot test A until B
               is built.

    To resolve this, we must reorder the graph edges:

    build A <-- test A <--> build B  <-- test B

    must become:

    build A  <-- build B <-- test A <-- test B
    """
    # find all test nodes with edges to build nodes
    test_nodes = [node for node in graph.nodes() if node.startswith('test-')]
    edges_from_test_to_build = [edge for edge in graph.edges() if edge[0] in test_nodes and
                                edge[1].startswith('build-')]

    # find any of their inverses.  Entries here are of the form (test-A, build-B)
    circular_deps = [edge for edge in edges_from_test_to_build
                     if (edge[1], edge[0]) in graph.edges()]

    for (testA, buildB) in circular_deps:
        # remove build B dependence on test A
        graph.remove_edge(testA, buildB)
        # remove test B dependence on build B
        testB = buildB.replace('build-', 'test-', 1)
        graph.remove_edge(buildB, testB)
        # Add test B dependence on test A
        graph.add_edge(testA, testB)
        # make sure that test A still depends on build B
        assert (buildB, testA) in graph.edges()
    # graph is modified in place.  No return necessary.

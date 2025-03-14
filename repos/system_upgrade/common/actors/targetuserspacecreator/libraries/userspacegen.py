import itertools
import os

from leapp import reporting
from leapp.exceptions import StopActorExecution, StopActorExecutionError
from leapp.libraries.actor import constants
from leapp.libraries.common import (
    dnfplugin,
    mounting,
    overlaygen,
    repofileutils,
    rhsm,
    rhui,
    utils,
)
from leapp.libraries.common.config import get_product_type, get_env
from leapp.libraries.common.config.version import get_target_major_version
from leapp.libraries.stdlib import CalledProcessError, api, config, run
from leapp.models import (
    CustomTargetRepositoryFile,
    RHUIInfo,
    RHSMInfo,
    RequiredTargetUserspacePackages,  # deprecated
    StorageInfo,
    TargetRepositories,
    TargetUserSpacePreupgradeTasks,
    TargetUserSpaceInfo,
    TMPTargetRepositoriesFacts,  # deprecated
    UsedTargetRepositories,
    UsedTargetRepository,
    XFSPresence,
)
from leapp.utils.deprecation import suppress_deprecation

# TODO: "refactor" (modify) the library significantly
# The current shape is really bad and ineffective (duplicit parsing
# of repofiles). The library is doing 3 (5) things:
# # (0.) consume process input data
# # 1. prepare the first container, to be able to obtain repositories for the
# #    target system (this is extra neededwhen rhsm is used, but not reason to
# #    do such thing only when rhsm is used. Be persistant here
# # 2. gather target repositories that should AND can be used
# #    - basically here is the main thing that is PITA; I started
# #      the refactoring but realized that it needs much more changes because
# #      of RHSM...
# # 3. create the target userspace bootstrap
# # (4.) produce messages with the data
#
# Because of the lack of time, I am extending the current bad situation,
# but after the release, the related code should be really refactored.
# It would be probably ideal, if this and other actors in the current and the
# next phase are modified properly and we could create inhibitors in the check
# phase and keep everything on the report. But currently it seems it doesn't
# worth to invest so much energy into it. So let's just make this really
# readable (includes split of the functionality into several libraries)
# and do not mess.
# Issue: #486

PROD_CERTS_FOLDER = 'prod-certs'
PERSISTENT_PACKAGE_CACHE_DIR = '/var/lib/leapp/persistent_package_cache'


def _check_deprecated_rhsm_skip():
    # we do not plan to cover this case by tests as it is purely
    # devel/testing stuff, that becomes deprecated now
    # just log the warning now (better than nothing?); deprecation process will
    # be specified in close future
    if get_env('LEAPP_DEVEL_SKIP_RHSM', '0') == '1':
        api.current_logger().warning(
            'The LEAPP_DEVEL_SKIP_RHSM has been deprecated. Use'
            ' LEAPP_NO_RHSM istead or use the --no-rhsm option for'
            ' leapp. as well custom repofile has not been defined.'
            ' Please read documentation about new "skip rhsm" solution.'
        )


class _InputData(object):
    def __init__(self):
        self._consume_data()

    @suppress_deprecation(RequiredTargetUserspacePackages)
    def _consume_data(self):
        """
        Wrapper function to consume majority input data.

        It doesn't consume TargetRepositories, which are consumed in the
        own function.
        """
        self.packages = {'dnf', 'dnf-command(config-manager)'}
        self.files = []
        _cftuples = set()

        def _update_files(copy_files):
            # add just uniq CopyFile objects to omit duplicate copying of files
            for cfile in copy_files:
                cftuple = (cfile.src, cfile.dst)
                if cftuple not in _cftuples:
                    _cftuples.add(cftuple)
                    self.files.append(cfile)

        for task in api.consume(TargetUserSpacePreupgradeTasks):
            self.packages.update(task.install_rpms)
            _update_files(task.copy_files)

        for message in api.consume(RequiredTargetUserspacePackages):
            self.packages.update(message.packages)

        # Get the RHSM information (available repos, attached SKUs, etc.) of the source system
        self.rhsm_info = next(api.consume(RHSMInfo), None)
        self.rhui_info = next(api.consume(RHUIInfo), None)
        if not self.rhsm_info and not rhsm.skip_rhsm():
            api.current_logger().warning('Could not receive RHSM information - Is this system registered?')
            raise StopActorExecution()
        if rhsm.skip_rhsm() and self.rhsm_info:
            # this should not happen. if so, raise an error as something in
            # other actors is wrong really
            raise StopActorExecutionError("RHSM is not handled but the RHSMInfo message has been produced.")

        self.custom_repofiles = list(api.consume(CustomTargetRepositoryFile))
        self.xfs_info = next(api.consume(XFSPresence), XFSPresence())
        self.storage_info = next(api.consume(StorageInfo), None)
        if not self.storage_info:
            raise StopActorExecutionError('No storage info available cannot proceed.')


def _restore_persistent_package_cache(userspace_dir):
    if get_env('LEAPP_DEVEL_USE_PERSISTENT_PACKAGE_CACHE', None) == '1':
        if os.path.exists(PERSISTENT_PACKAGE_CACHE_DIR):
            with mounting.NspawnActions(base_dir=userspace_dir) as target_context:
                target_context.copytree_to(PERSISTENT_PACKAGE_CACHE_DIR, '/var/cache/dnf')
    # We always want to remove the persistent cache here to unclutter the system
    run(['rm', '-rf', PERSISTENT_PACKAGE_CACHE_DIR])


def _backup_to_persistent_package_cache(userspace_dir):
    if get_env('LEAPP_DEVEL_USE_PERSISTENT_PACKAGE_CACHE', None) == '1':
        # Clean up any dead bodies, just in case
        run(['rm', '-rf', PERSISTENT_PACKAGE_CACHE_DIR])
        if os.path.exists(os.path.join(userspace_dir, 'var', 'cache', 'dnf')):
            with mounting.NspawnActions(base_dir=userspace_dir) as target_context:
                target_context.copytree_from('/var/cache/dnf', PERSISTENT_PACKAGE_CACHE_DIR)


def prepare_target_userspace(context, userspace_dir, enabled_repos, packages):
    """
    Implement the creation of the target userspace.
    """
    _backup_to_persistent_package_cache(userspace_dir)

    target_major_version = get_target_major_version()
    run(['rm', '-rf', userspace_dir])
    _create_target_userspace_directories(userspace_dir)
    with mounting.BindMount(
        source=userspace_dir, target=os.path.join(context.base_dir, 'el{}target'.format(target_major_version))
    ):
        _restore_persistent_package_cache(userspace_dir)

        repos_opt = [['--enablerepo', repo] for repo in enabled_repos]
        repos_opt = list(itertools.chain(*repos_opt))
        cmd = ['dnf',
               'install',
               '-y',
               '--nogpgcheck',
               '--setopt=module_platform_id=platform:el{}'.format(target_major_version),
               '--setopt=keepcache=1',
               '--releasever', api.current_actor().configuration.version.target,
               '--installroot', '/el{}target'.format(target_major_version),
               '--disablerepo', '*'
               ] + repos_opt + packages
        if config.is_verbose():
            cmd.append('-v')
        if rhsm.skip_rhsm():
            cmd += ['--disableplugin', 'subscription-manager']
        try:
            context.call(cmd, callback_raw=utils.logging_handler)
        except CalledProcessError as exc:
            raise StopActorExecutionError(
                message='Unable to install RHEL {} userspace packages.'.format(target_major_version),
                details={'details': str(exc), 'stderr': exc.stderr}
            )


def _get_all_rhui_pkgs():
    """
    Return the list of rhui packages

    Currently, do not care about what rhui we have, release, etc.
    Just take all packages. We need them just for the purpose of filtering
    what files we have to remove (see _prep_repository_access) and it's ok
    for us to use whatever rhui rpms (the relevant rpms catch the problem,
    the others are just taking bytes in memory...). It's a hot-fix. We are going
    to refactor the library later completely..
    """
    pkgs = []
    for rhui_map in rhui.RHUI_CLOUD_MAP[api.current_actor().configuration.architecture].values():
        for key in rhui_map.keys():
            if not key.endswith('pkg'):
                continue
            pkgs.append(rhui_map[key])
    return pkgs


def _get_files_owned_by_rpms(context, dirpath, pkgs=None):
    """
    Return the list of file names inside dirpath owned by RPMs.

    This is important e.g. in case of RHUI which installs specific repo files
    in the yum.repos.d directory.

    In case the pkgs param is None or empty, do not filter any specific rpms.
    Otherwise return filenames that are owned by any pkg in the given list.
    """
    files_owned_by_rpms = []
    for fname in os.listdir(context.full_path(dirpath)):
        try:
            result = context.call(['rpm', '-qf', os.path.join(dirpath, fname)])
        except CalledProcessError:
            api.current_logger().debug('SKIP the {} file: not owned by any rpm'.format(fname))
            continue
        if pkgs and not [pkg for pkg in pkgs if pkg in result['stdout']]:
            api.current_logger().debug('SKIP the {} file: not owned by any searched rpm:'.format(fname))
            continue
        api.current_logger().debug('Found the file owned by an rpm: {}.'.format(fname))
        files_owned_by_rpms.append(fname)
    return files_owned_by_rpms


def _prep_repository_access(context, target_userspace):
    """
    Prepare repository access by copying all relevant certificates and configuration files to the userspace
    """
    target_etc = os.path.join(target_userspace, 'etc')
    target_yum_repos_d = os.path.join(target_etc, 'yum.repos.d')
    backup_yum_repos_d = os.path.join(target_etc, 'yum.repos.d.backup')
    if not rhsm.skip_rhsm():
        run(['rm', '-rf', os.path.join(target_etc, 'pki')])
        run(['rm', '-rf', os.path.join(target_etc, 'rhsm')])
        context.copytree_from('/etc/pki', os.path.join(target_etc, 'pki'))
        context.copytree_from('/etc/rhsm', os.path.join(target_etc, 'rhsm'))
    # NOTE: we cannot just remove the original target yum.repos.d dir
    # as e.g. in case of RHUI a special RHUI repofiles are installed by a pkg
    # when the target userspace container is created. Removing these files we loose
    # RHUI target repositories. So ...->
    # -> detect such a files...
    with mounting.NspawnActions(base_dir=target_userspace) as target_context:
        files_owned_by_rpms = _get_files_owned_by_rpms(target_context, '/etc/yum.repos.d')

    # -> backup the orig dir & install the new one
    run(['mv', target_yum_repos_d, backup_yum_repos_d])
    context.copytree_from('/etc/yum.repos.d', target_yum_repos_d)

    # -> find old rhui repo files (we have to remove these as they cause duplicates)
    rhui_pkgs = _get_all_rhui_pkgs()
    old_files_owned_by_rhui_rpms = _get_files_owned_by_rpms(context, '/etc/yum.repos.d', rhui_pkgs)
    for fname in old_files_owned_by_rhui_rpms:
        api.current_logger().debug('Remove the old repofile: {}'.format(fname))
        run(['rm', '-f', os.path.join(target_yum_repos_d, fname)])
    # .. continue: remove our leapp rhui repo file (do not care if we are on rhui or not)
    for rhui_map in rhui.gen_rhui_files_map().values():
        for item in rhui_map:
            if item[1] != rhui.YUM_REPOS_PATH:
                continue
            target_leapp_repofile = os.path.join(target_yum_repos_d, item[0])
            if not os.path.isfile(target_leapp_repofile):
                continue
            # we found it!!
            run(['rm', '-f', target_leapp_repofile])
            break

    # -> copy expected files back
    for fname in files_owned_by_rpms:
        api.current_logger().debug('Copy the backed up repo file: {}'.format(fname))
        run(['mv', os.path.join(backup_yum_repos_d, fname), os.path.join(target_yum_repos_d, fname)])

    # -> remove the backed up dir
    run(['rm', '-rf', backup_yum_repos_d])


def _get_product_certificate_path():
    """
    Retrieve the required / used product certificate for RHSM.
    """
    architecture = api.current_actor().configuration.architecture
    target_version = api.current_actor().configuration.version.target
    target_product_type = get_product_type('target')
    certs_dir = api.get_common_folder_path(PROD_CERTS_FOLDER)

    # TODO: do we need EUS/... here or is it ga one enough to get eus repos?
    prod_certs = {
        'x86_64': {
            'ga': '479.pem',
            'beta': '486.pem',
            'htb': '230.pem',
        },
        'aarch64': {
            'ga': '419.pem',
            'beta': '363.pem',
            'htb': '489.pem',
        },
        'ppc64le': {
            'ga': '279.pem',
            'beta': '362.pem',
            'htb': '233.pem',
        },
        's390x': {
            'ga': '72.pem',
            'beta': '433.pem',
            'htb': '232.pem',
        }
    }

    try:
        cert = prod_certs[architecture][target_product_type]
    except KeyError as e:
        raise StopActorExecutionError(message=('Failed to determine what certificate to use for {}.'.format(e)))

    cert_path = os.path.join(certs_dir, target_version, cert)
    if not os.path.isfile(cert_path):
        details = {'missing certificate': cert, 'path': cert_path}
        if target_product_type != 'ga':
            details['hint'] = (
                'You chose to upgrade to beta or htb system but probably'
                ' chose version for which beta/htb certificates are not'
                ' attached (e.g. because the GA has been released already).'
                ' Set the target os version for which the {} certificate'
                ' is provided using the LEAPP_DEVEL_TARGET_RELEASE envar.'
                .format(cert)
            )
            details['search cmd'] = 'find {} | grep {}'.format(certs_dir, cert)
        raise StopActorExecutionError(
            message='Cannot find the product certificate file for the chosen target system.',
            details=details
        )
    return cert_path


def _create_target_userspace_directories(target_userspace):
    api.current_logger().debug('Creating target userspace directories.')
    try:
        utils.makedirs(target_userspace)
        api.current_logger().debug('Done creating target userspace directories.')
    except OSError:
        api.current_logger().error(
            'Failed to create temporary target userspace directories %s', target_userspace, exc_info=True)
        # This is an attempt for giving the user a chance to resolve it on their own
        raise StopActorExecutionError(
            message='Failed to prepare environment for package download while creating directories.',
            details={
                'hint': 'Please ensure that {directory} is empty and modifiable.'.format(directory=target_userspace)
            }
        )


def _inhibit_on_duplicate_repos(repofiles):
    """
    Inhibit the upgrade if any repoid is defined multiple times.

    When that happens, it not only shows misconfigured system, but then
    we can't get details of all the available repos as well.
    """
    # TODO: this is is duplicate of rhsm._inhibit_on_duplicate_repos
    # Issue: #486
    duplicates = repofileutils.get_duplicate_repositories(repofiles).keys()

    if not duplicates:
        return
    list_separator_fmt = '\n    - '
    api.current_logger().warning(
        'The following repoids are defined multiple times:{0}{1}'
        .format(list_separator_fmt, list_separator_fmt.join(duplicates))
    )

    reporting.create_report([
        reporting.Title('A YUM/DNF repository defined multiple times'),
        reporting.Summary(
            'The following repositories are defined multiple times inside the'
            ' "upgrade" container:{0}{1}'
            .format(list_separator_fmt, list_separator_fmt.join(duplicates))
        ),
        reporting.Severity(reporting.Severity.MEDIUM),
        reporting.Tags([reporting.Tags.REPOSITORY]),
        reporting.Flags([reporting.Flags.INHIBITOR]),
        reporting.Remediation(hint=(
            'Remove the duplicate repository definitions or change repoids of'
            ' conflicting repositories on the system to prevent the'
            ' conflict.'
            )
        )
    ])


def _get_all_available_repoids(context):
    repofiles = repofileutils.get_parsed_repofiles(context)
    # TODO: this is not good solution, but keep it as it is now
    # Issue: #486
    if rhsm.skip_rhsm():
        # only if rhsm is skipped, the duplicate repos are not detected
        # automatically and we need to do it extra
        _inhibit_on_duplicate_repos(repofiles)
    repoids = []
    for rfile in repofiles:
        if rfile.data:
            repoids += [repo.repoid for repo in rfile.data]
    return set(repoids)


def _get_rhsm_available_repoids(context):
    target_major_version = get_target_major_version()
    # FIXME: check that required repo IDs (baseos, appstream)
    # + or check that all required RHEL repo IDs are available.
    if rhsm.skip_rhsm():
        return set()
    # Get the RHSM repos available in the target RHEL container
    # TODO: very similar thing should happens for all other repofiles in container
    #
    repoids = rhsm.get_available_repo_ids(context)
    if not repoids or len(repoids) < 2:
        raise StopActorExecutionError(
            message='Cannot find required basic RHEL {} repositories.'.format(target_major_version),
            details={
                'hint': ('It is required to have RHEL repositories on the system'
                         ' provided by the subscription-manager unless the --no-rhsm'
                         ' options is specified. Possibly you'
                         ' are missing a valid SKU for the target system or network'
                         ' connection failed. Check whether your system is attached'
                         ' to a valid SKU providing RHEL {} repositories.'
                         ' In case the Satellite is used, read the upgrade documentation'
                         ' to setup the satellite and the system properly.'.format(target_major_version))
            }
        )
    return set(repoids)


def _get_rhui_available_repoids(context, cloud_repo):
    repofiles = repofileutils.get_parsed_repofiles(context)

    # TODO: same refactoring as Issue #486?
    _inhibit_on_duplicate_repos(repofiles)
    repoids = []
    for rfile in repofiles:
        if rfile.file == cloud_repo and rfile.data:
            repoids = [repo.repoid for repo in rfile.data]
            repoids.sort()
            break
    return set(repoids)


def _get_rh_available_repoids(context, indata):
    """
    RH repositories are provided either by RHSM or are stored in the expected repo file provided by
    RHUI special packages (every cloud provider has itw own rpm).
    """

    arch = api.current_actor().configuration.architecture

    rh_repoids = _get_rhsm_available_repoids(context)

    if indata and indata.rhui_info:
        cloud_repo = os.path.join(
            '/etc/yum.repos.d/', rhui.RHUI_CLOUD_MAP[arch][indata.rhui_info.provider]['leapp_pkg_repo']
        )
        rhui_repoids = _get_rhui_available_repoids(context, cloud_repo)
        rh_repoids.update(rhui_repoids)

    return rh_repoids


def gather_target_repositories(context, indata):
    """
    Get available required target repositories and inhibit or raise error if basic checks do not pass.

    In case of repositories provided by Red Hat, it's checked whether the basic
    required repositories are available (or at least defined) in the given
    context. If not, raise StopActorExecutionError.

    For the custom target repositories we expect all of them have to be defined.
    If any custom target repository is missing, raise StopActorExecutionError.

    If any repository is defined multiple times, produce the inhibitor Report
    msg.

    :param context: An instance of a mounting.IsolatedActions class
    :type context: mounting.IsolatedActions class
    :return: List of target system repoids
    :rtype: List(string)
    """
    rh_available_repoids = _get_rh_available_repoids(context, indata)
    all_available_repoids = _get_all_available_repoids(context)

    target_repoids = []
    missing_custom_repoids = []
    for target_repo in api.consume(TargetRepositories):
        for rhel_repo in target_repo.rhel_repos:
            if rhel_repo.repoid in rh_available_repoids:
                target_repoids.append(rhel_repo.repoid)
            else:
                # TODO: We shall report that the RHEL repos that we deem necessary for
                # the upgrade are not available; but currently it would just print bunch of
                # data everytime as we maps EUS and other repositories as well. But these
                # do not have to be necessary available on the target system in the time
                # of the upgrade. Let's skip it for now until it's clear how we will deal
                # with it.
                pass
        for custom_repo in target_repo.custom_repos:
            if custom_repo.repoid in all_available_repoids:
                target_repoids.append(custom_repo.repoid)
            else:
                missing_custom_repoids.append(custom_repo.repoid)
    api.current_logger().debug("Gathered target repositories: {}".format(', '.join(target_repoids)))
    if not target_repoids:
        raise StopActorExecutionError(
            message='There are no enabled target repositories for the upgrade process to proceed.',
            details={'hint': (
                'Ensure your system is correctly registered with the subscription manager and that'
                ' your current subscription is entitled to install the requested target version {version}.'
                ' In case the --no-rhsm option (or the LEAPP_NO_RHSM=1 environment variable is set)'
                ' ensure the custom repository file is provided regarding the documentation with'
                ' properly defined repositories or in case repositories are already defined'
                ' in any repofiles under /etc/yum.repos.d/ directory, use the --enablerepo option'
                ' for leapp. Also make sure "/etc/leapp/files/repomap.csv" file is up-to-date.'
                ).format(version=api.current_actor().configuration.version.target)
            }
        )
    if missing_custom_repoids:
        raise StopActorExecutionError(
            message='Some required custom target repositories are not available.',
            details={'hint': (
                ' The most probably you are using custom or third party actor'
                ' that produces CustomTargetRepository message or you did a typo'
                ' in one of repoids specified on command line for the leapp --enablerepo'
                ' option.'
                ' Inside the upgrade container, we are not able to find such'
                ' repository inside any repository file. Consider use of the'
                ' custom repository file regarding the official upgrade'
                ' documentation or check whether you did not do a typo in any'
                ' repoids you specified for the --enablerepo option of leapp.'
                )
            }
        )

    return set(target_repoids)


def _install_custom_repofiles(context, custom_repofiles):
    """
    Install the required custom repository files into the container.

    The repostory files are copied from the host into the /etc/yum.repos.d
    directory into the container.

    :param context: the container where the repofiles should be copied
    :type context: mounting.IsolatedActions class
    :param custom_repofiles: list of custom repo files
    :type custom_repofiles: List(CustomTargetRepositoryFile)
    """
    for rfile in custom_repofiles:
        _dst_path = os.path.join('/etc/yum.repos.d', os.path.basename(rfile.file))
        context.copy_to(rfile.file, _dst_path)


def _gather_target_repositories(context, indata, prod_cert_path):
    """
    This is wrapper function to gather the target repoids.

    Probably the function could be partially merged into gather_target_repositories
    and this could be really just wrapper with the switch of certificates.
    I am keeping that for now as it is as interim step.

    :param context: the container where the repofiles should be copied
    :type context: mounting.IsolatedActions class
    :param indata: majority of input data for the actor
    :type indata: class _InputData
    :param prod_cert_path: path where the target product cert is stored
    :type prod_cert_path: string
    """
    rhsm.set_container_mode(context)
    rhsm.switch_certificate(context, indata.rhsm_info, prod_cert_path)
    if indata.rhui_info:
        rhui.copy_rhui_data(context, indata.rhui_info.provider)
    _install_custom_repofiles(context, indata.custom_repofiles)
    return gather_target_repositories(context, indata)


def _copy_files(context, files):
    """
    Copy the files/dirs from the host to the `context` userspace

    :param context: An instance of a mounting.IsolatedActions class
    :type context: mounting.IsolatedActions class
    :param files: list of files that should be copied from the host to the context
    :type files: list of CopyFile
    """
    for file_task in files:
        if not file_task.dst:
            file_task.dst = file_task.src
        if os.path.isdir(file_task.src):
            context.remove_tree(file_task.dst)
            context.copytree_to(file_task.src, file_task.dst)
        else:
            context.copy_to(file_task.src, file_task.dst)


def _get_target_userspace():
    return constants.TARGET_USERSPACE.format(get_target_major_version())


def _create_target_userspace(context, packages, files, target_repoids):
    """Create the target userspace."""
    target_path = _get_target_userspace()
    prepare_target_userspace(context, target_path, target_repoids, list(packages))
    _prep_repository_access(context, target_path)

    with mounting.NspawnActions(base_dir=target_path) as target_context:
        _copy_files(target_context, files)
    dnfplugin.install(_get_target_userspace())

    # and do not forget to set the rhsm into the container mode again
    with mounting.NspawnActions(_get_target_userspace()) as target_context:
        rhsm.set_container_mode(target_context)


@suppress_deprecation(TMPTargetRepositoriesFacts)
def perform():
    # NOTE: this one action is out of unit-tests completely; we do not use
    # in unit tests the LEAPP_DEVEL_SKIP_RHSM envar anymore
    _check_deprecated_rhsm_skip()

    indata = _InputData()
    prod_cert_path = _get_product_certificate_path()
    with overlaygen.create_source_overlay(
            mounts_dir=constants.MOUNTS_DIR,
            scratch_dir=constants.SCRATCH_DIR,
            storage_info=indata.storage_info,
            xfs_info=indata.xfs_info) as overlay:
        with overlay.nspawn() as context:
            target_repoids = _gather_target_repositories(context, indata, prod_cert_path)
            _create_target_userspace(context, indata.packages, indata.files, target_repoids)
            # TODO: this is tmp solution as proper one needs significant refactoring
            target_repo_facts = repofileutils.get_parsed_repofiles(context)
            api.produce(TMPTargetRepositoriesFacts(repositories=target_repo_facts))
            # ## TODO ends here
            api.produce(UsedTargetRepositories(
                repos=[UsedTargetRepository(repoid=repo) for repo in target_repoids]))
            api.produce(TargetUserSpaceInfo(
                path=_get_target_userspace(),
                scratch=constants.SCRATCH_DIR,
                mounts=constants.MOUNTS_DIR))

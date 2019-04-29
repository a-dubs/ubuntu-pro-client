import logging
import os
import re

try:
    from typing import Any, Dict  # noqa: F401
except ImportError:
    # typing isn't available on trusty, so ignore its absence
    pass


from uaclient import apt
from uaclient.entitlements import base
from uaclient import status
from uaclient import util

APT_DISABLED_PIN = '-32768'


class RepoEntitlement(base.UAEntitlement):

    repo_list_file_tmpl = '/etc/apt/sources.list.d/ubuntu-{name}-{series}.list'
    repo_pref_file_tmpl = '/etc/apt/preferences.d/ubuntu-{name}-{series}'
    origin = None   # The repo Origin value for setting pinning

    repo_url = 'UNSET'
    repo_key_file = 'UNSET'   # keyfile delivered by ubuntu-cloudimage-keyring
    repo_pin_priority = None  # Optional repo pin priority in subclass

    # force_disable True if entitlement does not allow disable (fips*)
    force_disable = False

    # disable_apt_auth_only (ESM) to only remove apt auth files on disable
    disable_apt_auth_only = False  # Set True on ESM to only remove apt auth

    # Any custom messages to emit pre or post enable or disable operations
    messaging = {}  # Currently post_enable is used in CommonCriteria
    packages = []   # Debs to install on enablement

    def enable(self, *, silent_if_inapplicable: bool = False) -> bool:
        """Enable specific entitlement.

        :param silent_if_inapplicable:
            Don't emit any messages until after it has been determined that
            this entitlement is applicable to the current machine.

        @return: True on success, False otherwise.
        """
        if not self.can_enable(silent=silent_if_inapplicable):
            return False
        if not self.setup_apt_config():
            return False
        if self.packages:
            try:
                print(
                    'Installing {title} packages ...'.format(title=self.title))
                util.subp(
                    ['apt-get', 'install', '--assume-yes'] + self.packages,
                    capture=True)
            except util.ProcessExecutionError:
                self.disable(silent=True, force=True)
                logging.error(
                    status.MESSAGE_ENABLED_FAILED_TMPL.format(
                        title=self.title))
                return False
        self._set_local_enabled(True)
        print(status.MESSAGE_ENABLED_TMPL.format(title=self.title))
        for msg in self.messaging.get('post_enable', []):
            print(msg)
        return True

    def disable(self, silent=False, force=False):
        if not self.can_disable(silent, force):
            return False
        if any([not self.force_disable, force]):
            self.remove_apt_config()
            try:
                util.subp(
                    ['apt-get', 'remove', '--assume-yes'] + self.packages)
            except util.ProcessExecutionError:
                pass
            self._set_local_enabled(False)
        if self.force_disable:
            if not silent:
                print('Warning: no option to disable {title}'.format(
                    title=self.title)
                )
            return False
        if not silent:
            print(status.MESSAGE_DISABLED_TMPL.format(title=self.title))
        return True

    def operational_status(self):
        """Return operational status of RepoEntitlement."""
        passed_affordances, details = self.check_affordances()
        if not passed_affordances:
            return status.INAPPLICABLE, details
        entitlement_cfg = self.cfg.entitlements.get(self.name)
        if not entitlement_cfg:
            return status.INACTIVE, '%s is not configured' % self.title
        directives = entitlement_cfg['entitlement'].get('directives', {})
        repo_url = directives.get('aptURL')
        if not repo_url:
            repo_url = self.repo_url
        protocol, repo_path = repo_url.split('://')
        out, _err = util.subp(['apt-cache', 'policy'])
        match = re.search(r'(?P<pin>(-)?\d+) %s' % repo_url, out)
        if match and match.group('pin') != APT_DISABLED_PIN:
            return status.ACTIVE, '%s is active' % self.title
        if os.getuid() != 0 and entitlement_cfg.get('localEnabled', False):
            # Use our cached enabled key for non-root users because apt
            # policy will show APT_DISABLED_PIN for authenticated sources
            return status.ACTIVE, '%s is active' % self.title
        return status.INACTIVE, '%s is not configured' % self.title

    def process_contract_deltas(
            self, orig_access: 'Dict[str, Any]',
            deltas: 'Dict[str, Any]') -> None:
        """Process any contract access deltas for this entitlement.

        :param orig_access: Dictionary containing the original
            resourceEntitlement access details.
        :param deltas: Dictionary which contains only the changed access keys
        and values.

        :return: True when delta operations are processed; False when noop.
        """
        if super().process_contract_deltas(orig_access, deltas):
            return True  # Already processed parent class deltas

        op_status, _details = self.operational_status()
        resourceToken = orig_access.get('resourceToken')
        if not resourceToken:
            resourceToken = deltas.get('resourceToken')
        delta_entitlement = deltas.get('entitlement', {})
        delta_obligations = delta_entitlement.get('obligations', {})
        enableByDefault = bool(
            delta_obligations.get('enableByDefault') and resourceToken)
        if not any([op_status == status.ACTIVE, enableByDefault]):
            return True
        if op_status == status.ACTIVE:
            logging.info(
                "Updating '%s' apt sources list on changed directives." %
                self.name)
        elif enableByDefault:
            msg = status.MESSAGE_ENABLE_BY_DEFAULT_TMPL.format(
                name=self.name)
            logging.info(msg)
        old_url = orig_access.get('directives', {}).get('aptURL')
        if old_url:
            # Remove original aptURL and auth and rewrite
            series = util.get_platform_info('series')
            repo_filename = self.repo_list_file_tmpl.format(
                name=self.name, series=series)
            apt.remove_auth_apt_repo(repo_filename, old_url)
        self.remove_apt_config()
        self.setup_apt_config()
        return True

    def setup_apt_config(self):
        series = util.get_platform_info('series')
        repo_filename = self.repo_list_file_tmpl.format(
            name=self.name, series=series)
        resource_cfg = self.cfg.entitlements.get(self.name)
        directives = resource_cfg['entitlement'].get('directives', {})
        token = resource_cfg.get('resourceToken')
        if not token:
            logging.debug(
                'No specific resourceToken present. Using machine token'
                ' as %s credentials', self.title)
            token = self.cfg.machine_token['machineToken']
        if directives.get('aptKey'):
            logging.debug(
                "Ignoring aptKey directive '%s'", directives.get('aptKey'))
        keyring_file = os.path.join(apt.KEYRINGS_DIR, self.repo_key_file)
        repo_url = directives.get('aptURL')
        if not repo_url:
            repo_url = self.repo_url
        repo_suites = directives.get('suites')
        if not repo_suites:
            logging.error(
                'Empty %s apt suites directive from %s',
                self.name, self.cfg.contract_url)
            return False
        if self.repo_pin_priority:
            if not self.origin:
                logging.error(
                    "Cannot setup apt pin. Empty apt repo origin value '%s'." %
                    self.origin)
                logging.error(
                    status.MESSAGE_ENABLED_FAILED_TMPL.format(
                        title=self.title))
                return False
            repo_pref_file = self.repo_pref_file_tmpl.format(
                name=self.name, series=series)
            apt.add_ppa_pinning(
                repo_pref_file, repo_url, self.origin, self.repo_pin_priority)

        prerequisite_pkgs = []
        if not os.path.exists(apt.APT_METHOD_HTTPS_FILE):
            prerequisite_pkgs.append('apt-transport-https')
        if not os.path.exists(apt.CA_CERTIFICATES_FILE):
            prerequisite_pkgs.append('ca-certificates')

        if prerequisite_pkgs:
            print('Installing prerequisites: {}'.format(
                ', '.join(prerequisite_pkgs)))
            try:
                util.subp(
                    ['apt-get', 'install', '--assume-yes'] + prerequisite_pkgs,
                    capture=True)
            except util.ProcessExecutionError as e:
                logging.error(str(e))
                return False
        try:
            apt.add_auth_apt_repo(
                repo_filename, repo_url, token, repo_suites,
                keyring_file)
        except apt.InvalidAPTCredentialsError as e:
            logging.error(str(e))
            return False
        # Run apt-update on any repo-entitlement enable because the machine
        # probably wants access to the repo that was just enabled.
        # Side-effect is that apt policy will new report the repo as accessible
        # which allows ua status to report correct info
        print('Updating package lists ...')
        util.subp(['apt-get', 'update'], capture=True)
        return True

    def remove_apt_config(self):
        """Remove any repository apt configuration files."""
        series = util.get_platform_info('series')
        repo_filename = self.repo_list_file_tmpl.format(
            name=self.name, series=series)
        keyring_file = os.path.join(apt.APT_KEYS_DIR, self.repo_key_file)
        entitlement = self.cfg.read_cache(
            'machine-access-%s' % self.name).get('entitlement', {})
        access_directives = entitlement.get('directives', {})
        repo_url = access_directives.get('aptURL', self.repo_url)
        if not repo_url:
            repo_url = self.repo_url
        if self.disable_apt_auth_only:
            # We only remove the repo from the apt auth file, because ESM
            # is a special-case: we want to be able to report on the
            # available ESM updates even when it's disabled
            apt.remove_repo_from_apt_auth_file(repo_url)
        else:
            apt.remove_auth_apt_repo(repo_filename, repo_url, keyring_file)
            apt.remove_apt_list_files(repo_url, series)
        if self.repo_pin_priority:
            repo_pref_file = self.repo_pref_file_tmpl.format(
                name=self.name, series=series)
            if os.path.exists(repo_pref_file):
                os.unlink(repo_pref_file)

    def _set_local_enabled(self, value):
        """Set local enabled flag true or false."""
        public_cache = self.cfg.read_cache('machine-access-%s' % self.name)
        public_cache['localEnabled'] = value
        redacted_cache = util.redact_sensitive(public_cache)
        self.cfg.write_cache(
            'machine-access-%s' % self.name, redacted_cache, private=False)

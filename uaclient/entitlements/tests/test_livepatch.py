"""Tests related to uaclient.entitlement.livepatch module."""

import copy
import logging
from functools import partial

import mock
import pytest

from uaclient import apt, exceptions, livepatch, messages
from uaclient.entitlements.entitlement_status import (
    ApplicabilityStatus,
    ApplicationStatus,
    CanEnableFailureReason,
    ContractStatus,
    UserFacingStatus,
)
from uaclient.entitlements.livepatch import (
    LivepatchEntitlement,
    process_config_directives,
)
from uaclient.snap import SNAP_CMD
from uaclient.testing import fakes

M_PATH = "uaclient.entitlements.livepatch."  # mock path
M_LIVEPATCH_STATUS = M_PATH + "LivepatchEntitlement.application_status"
DISABLED_APP_STATUS = (ApplicationStatus.DISABLED, "")

M_BASE_PATH = "uaclient.entitlements.base.UAEntitlement."

DEFAULT_AFFORDANCES = {
    "architectures": ["x86_64"],
    "minKernelVersion": "4.4",
    "kernelFlavors": ["generic", "lowlatency"],
    "tier": "stable",
}


@pytest.fixture
def livepatch_entitlement_factory(entitlement_factory):
    directives = {"caCerts": "", "remoteServer": "https://alt.livepatch.com"}
    return partial(
        entitlement_factory,
        LivepatchEntitlement,
        affordances=DEFAULT_AFFORDANCES,
        directives=directives,
    )


@pytest.fixture
def entitlement(livepatch_entitlement_factory):
    return livepatch_entitlement_factory()


class TestLivepatchContractStatus:
    def test_contract_status_entitled(self, entitlement):
        """The contract_status returns ENTITLED when entitled is True."""
        assert ContractStatus.ENTITLED == entitlement.contract_status()

    def test_contract_status_unentitled(self, livepatch_entitlement_factory):
        """The contract_status returns NONE when entitled is False."""
        entitlement = livepatch_entitlement_factory(entitled=False)
        assert ContractStatus.UNENTITLED == entitlement.contract_status()


class TestLivepatchUserFacingStatus:
    @mock.patch(
        "uaclient.livepatch.on_supported_kernel",
        return_value=None,
    )
    @mock.patch(
        "uaclient.entitlements.livepatch.system.is_container",
        return_value=True,
    )
    def test_user_facing_status_inapplicable_on_inapplicable_status(
        self,
        _m_is_container,
        _m_on_supported_kernel,
        livepatch_entitlement_factory,
    ):
        """The user-facing details INAPPLICABLE applicability_status"""
        affordances = copy.deepcopy(DEFAULT_AFFORDANCES)
        affordances["series"] = ["bionic"]

        entitlement = livepatch_entitlement_factory(affordances=affordances)

        with mock.patch(
            "uaclient.system.get_release_info"
        ) as m_get_release_info:
            m_get_release_info.return_value = mock.MagicMock(series="xenial")
            uf_status, details = entitlement.user_facing_status()
        assert uf_status == UserFacingStatus.INAPPLICABLE
        expected_details = "Cannot install Livepatch on a container."
        assert expected_details == details.msg


class TestLivepatchProcessConfigDirectives:
    @pytest.mark.parametrize(
        "directive_key,livepatch_param_tmpl",
        (("remoteServer", "remote-server={}"), ("caCerts", "ca-certs={}")),
    )
    def test_call_livepatch_config_command(
        self, directive_key, livepatch_param_tmpl
    ):
        """Livepatch config directives are passed to livepatch config."""
        directive_value = "{}-value".format(directive_key)
        cfg = {"entitlement": {"directives": {directive_key: directive_value}}}
        with mock.patch("uaclient.system.subp") as m_subp:
            process_config_directives(cfg)
        expected_subp = mock.call(
            [
                livepatch.LIVEPATCH_CMD,
                "config",
                livepatch_param_tmpl.format(directive_value),
            ],
            capture=True,
        )
        assert [expected_subp] == m_subp.call_args_list

    def test_handle_multiple_directives(self):
        """Handle multiple Livepatch directives using livepatch config."""
        cfg = {
            "entitlement": {
                "directives": {
                    "remoteServer": "value1",
                    "caCerts": "value2",
                    "ignored": "ignoredvalue",
                }
            }
        }
        with mock.patch("uaclient.system.subp") as m_subp:
            process_config_directives(cfg)
        expected_calls = [
            mock.call(
                [livepatch.LIVEPATCH_CMD, "config", "ca-certs=value2"],
                capture=True,
            ),
            mock.call(
                [livepatch.LIVEPATCH_CMD, "config", "remote-server=value1"],
                capture=True,
            ),
        ]
        assert expected_calls == m_subp.call_args_list

    @pytest.mark.parametrize("directives", ({}, {"otherkey": "othervalue"}))
    def test_ignores_other_or_absent(self, directives):
        """Ignore empty or unexpected directives and do not call livepatch."""
        cfg = {"entitlement": {"directives": directives}}
        with mock.patch("uaclient.system.subp") as m_subp:
            process_config_directives(cfg)
        assert 0 == m_subp.call_count


@mock.patch(
    "uaclient.entitlements.fips.FIPSEntitlement.application_status",
    return_value=DISABLED_APP_STATUS,
)
@mock.patch(M_LIVEPATCH_STATUS, return_value=DISABLED_APP_STATUS)
@mock.patch(
    "uaclient.entitlements.livepatch.system.is_container", return_value=False
)
class TestLivepatchEntitlementCanEnable:
    @mock.patch(
        "uaclient.system.get_release_info",
        return_value=mock.MagicMock(series="xenial"),
    )
    @mock.patch(
        "uaclient.system.get_kernel_info",
        return_value=mock.MagicMock(uname_release="4.2.9-00-generic"),
    )
    def test_can_enable_false_on_containers(
        self,
        _m_get_kernel_info,
        _m_get_release_info,
        m_is_container,
        _m_livepatch_status,
        _m_fips_status,
        entitlement,
    ):
        """When is_container is True, can_enable returns False."""
        m_is_container.return_value = True
        entitlement = LivepatchEntitlement(entitlement.cfg)
        result, reason = entitlement.can_enable()
        assert False is result
        assert CanEnableFailureReason.INAPPLICABLE == reason.reason
        msg = "Cannot install Livepatch on a container."
        assert msg == reason.message.msg


class TestLivepatchProcessContractDeltas:
    @mock.patch(M_PATH + "LivepatchEntitlement.setup_livepatch_config")
    def test_true_on_parent_process_deltas(
        self, m_setup_livepatch_config, entitlement
    ):
        """When parent's process_contract_deltas returns True do no setup."""
        assert entitlement.process_contract_deltas({}, {}, False)
        assert [] == m_setup_livepatch_config.call_args_list

    @mock.patch(M_PATH + "LivepatchEntitlement.setup_livepatch_config")
    @mock.patch(M_PATH + "LivepatchEntitlement.application_status")
    @mock.patch(M_PATH + "LivepatchEntitlement.applicability_status")
    def test_false_on_inactive_livepatch_service(
        self,
        m_applicability_status,
        m_application_status,
        m_setup_livepatch_config,
        entitlement,
    ):
        """When livepatch is INACTIVE return False and do no setup."""
        m_applicability_status.return_value = (
            ApplicabilityStatus.APPLICABLE,
            "",
        )
        m_application_status.return_value = (
            ApplicationStatus.DISABLED,
            "",
        )
        deltas = {"entitlement": {"directives": {"caCerts": "new"}}}
        assert not entitlement.process_contract_deltas({}, deltas, False)
        assert [] == m_setup_livepatch_config.call_args_list

    @pytest.mark.parametrize(
        "directives,process_directives,process_token",
        (
            ({"caCerts": "new"}, True, False),
            ({"remoteServer": "new"}, True, False),
            ({"unhandledKey": "new"}, False, False),
        ),
    )
    @mock.patch(M_PATH + "LivepatchEntitlement.setup_livepatch_config")
    @mock.patch(M_PATH + "LivepatchEntitlement.application_status")
    def test_setup_performed_when_active_and_supported_deltas(
        self,
        m_application_status,
        m_setup_livepatch_config,
        entitlement,
        directives,
        process_directives,
        process_token,
    ):
        """Run setup when livepatch ACTIVE and deltas are supported keys."""
        application_status = ApplicationStatus.ENABLED
        m_application_status.return_value = (application_status, "")
        deltas = {"entitlement": {"directives": directives}}
        assert entitlement.process_contract_deltas({}, deltas, False)
        if any([process_directives, process_token]):
            setup_calls = [
                mock.call(
                    progress=mock.ANY,
                    process_directives=process_directives,
                    process_token=process_token,
                )
            ]
        else:
            setup_calls = []
        assert setup_calls == m_setup_livepatch_config.call_args_list

    @pytest.mark.parametrize(
        "deltas,process_directives,process_token",
        (
            ({"entitlement": {"something": 1}}, False, False),
            ({"resourceToken": "new"}, False, True),
        ),
    )
    @mock.patch(M_PATH + "LivepatchEntitlement.setup_livepatch_config")
    @mock.patch(M_PATH + "LivepatchEntitlement.application_status")
    def test_livepatch_disable_and_setup_performed_when_resource_token_changes(
        self,
        m_application_status,
        m_setup_livepatch_config,
        entitlement,
        deltas,
        process_directives,
        process_token,
    ):
        """Run livepatch calls setup when resourceToken changes."""
        application_status = ApplicationStatus.ENABLED
        m_application_status.return_value = (application_status, "")
        entitlement.process_contract_deltas({}, deltas, False)
        if any([process_directives, process_token]):
            setup_calls = [
                mock.call(
                    progress=mock.ANY,
                    process_directives=process_directives,
                    process_token=process_token,
                )
            ]
        else:
            setup_calls = []
        assert setup_calls == m_setup_livepatch_config.call_args_list

    @mock.patch(M_PATH + "LivepatchEntitlement.enable")
    @mock.patch(M_PATH + "LivepatchEntitlement.application_status")
    def test_livepatch_disable_and_delta_with_enable_by_default(
        self,
        m_application_status,
        m_enable,
        entitlement,
    ):
        deltas = {"entitlement": {"obligations": {"enableByDefault": True}}}
        m_enable.return_value = (True, None)
        application_status = ApplicationStatus.DISABLED
        m_application_status.return_value = (application_status, "")
        entitlement.process_contract_deltas({}, deltas, False)
        assert [mock.call(mock.ANY)] == m_enable.call_args_list


@mock.patch(M_PATH + "snap.is_snapd_installed_as_a_snap")
@mock.patch(M_PATH + "snap.is_snapd_installed")
@mock.patch("uaclient.http.validate_proxy", side_effect=lambda x, y, z: y)
@mock.patch("uaclient.snap.configure_snap_proxy")
@mock.patch("uaclient.livepatch.configure_livepatch_proxy")
class TestLivepatchEntitlementEnable:

    mocks_apt_update = []
    mocks_snapd_install = [
        mock.call(
            ["apt-get", "install", "--assume-yes", "snapd"],
            retry_sleeps=apt.APT_RETRIES,
        )
    ]
    mocks_snapd_install_as_a_snap = [
        mock.call(
            ["/usr/bin/snap", "install", "snapd"],
            capture=True,
            retry_sleeps=[0.5, 1, 5],
        )
    ]
    mocks_snap_wait_seed = [
        mock.call(
            ["/usr/bin/snap", "wait", "system", "seed.loaded"], capture=True
        )
    ]
    mocks_snapd_refresh = [
        mock.call(
            ["/usr/bin/snap", "refresh", "snapd"],
            capture=True,
        )
    ]
    mocks_livepatch_install = [
        mock.call(
            ["/usr/bin/snap", "install", "canonical-livepatch"],
            capture=True,
            retry_sleeps=[0.5, 1, 5],
        )
    ]
    mocks_install = (
        mocks_snapd_install
        + mocks_snapd_install_as_a_snap
        + mocks_snap_wait_seed
        + mocks_snapd_refresh
        + mocks_livepatch_install
    )
    mocks_config = [
        mock.call(
            [
                livepatch.LIVEPATCH_CMD,
                "config",
                "remote-server=https://alt.livepatch.com",
            ],
            capture=True,
        ),
        mock.call([livepatch.LIVEPATCH_CMD, "disable"]),
        mock.call(
            [livepatch.LIVEPATCH_CMD, "enable", "livepatch-token"],
            capture=True,
        ),
    ]

    @pytest.mark.parametrize("caplog_text", [logging.DEBUG], indirect=True)
    @pytest.mark.parametrize("apt_update_success", (True, False))
    @mock.patch("uaclient.system.get_release_info")
    @mock.patch("uaclient.system.subp")
    @mock.patch("uaclient.contract.apply_contract_overrides")
    @mock.patch("uaclient.apt.update_sources_list")
    @mock.patch("uaclient.apt.run_apt_install_command")
    @mock.patch("uaclient.apt.run_apt_update_command")
    @mock.patch("uaclient.system.which", return_value=None)
    @mock.patch(M_PATH + "LivepatchEntitlement.application_status")
    @mock.patch(
        M_PATH + "LivepatchEntitlement.can_enable", return_value=(True, None)
    )
    def test_enable_installs_snapd_and_livepatch_snap_when_absent(
        self,
        m_can_enable,
        m_app_status,
        m_which,
        m_run_apt_update,
        m_run_apt_install,
        m_update_sources_list,
        _m_contract_overrides,
        m_subp,
        _m_get_release_info,
        m_livepatch_proxy,
        m_snap_proxy,
        m_validate_proxy,
        m_is_snapd_installed,
        m_is_snapd_installed_as_a_snap,
        capsys,
        caplog_text,
        event,
        entitlement,
        apt_update_success,
    ):
        """Install snapd and canonical-livepatch snap when not on system."""
        application_status = ApplicationStatus.ENABLED
        m_app_status.return_value = application_status, "enabled"
        m_is_snapd_installed.return_value = False
        m_is_snapd_installed_as_a_snap.return_value = False

        def fake_update_sources_list(sources_list):
            if apt_update_success:
                return
            raise fakes.FakeUbuntuProError()

        m_update_sources_list.side_effect = fake_update_sources_list

        progress_mock = mock.MagicMock()
        assert entitlement.enable(progress_mock)
        assert self.mocks_install + self.mocks_config in m_subp.call_args_list
        assert self.mocks_apt_update == m_run_apt_update.call_args_list
        assert 1 == m_update_sources_list.call_count
        assert [
            mock.call("Installing Livepatch"),
            mock.call("Setting up Livepatch"),
        ] == progress_mock.progress.call_args_list
        assert [
            mock.call("message_operation", None),
            mock.call("message_operation", None),
            mock.call("info", "Installing snapd"),
            mock.call("info", "Installing snapd snap"),
            mock.call("info", "Installing canonical-livepatch snap"),
            mock.call(
                "info", "Disabling Livepatch prior to re-attach with new token"
            ),
        ] == progress_mock.emit.call_args_list
        assert [mock.call(livepatch.LIVEPATCH_CMD)] == m_which.call_args_list
        expected_log = (
            "DEBUG    Trying to install snapd."
            " Ignoring apt-get update failure: This is a test"
        )
        if apt_update_success:
            assert expected_log not in caplog_text()
        else:
            assert expected_log in caplog_text()
        assert [mock.call(livepatch.LIVEPATCH_CMD)] == m_which.call_args_list
        assert m_validate_proxy.call_count == 2
        assert m_snap_proxy.call_count == 1
        assert m_livepatch_proxy.call_count == 1

    @pytest.mark.parametrize("caplog_text", [logging.DEBUG], indirect=True)
    @mock.patch("uaclient.system.get_release_info")
    @mock.patch("uaclient.system.subp")
    @mock.patch("uaclient.contract.apply_contract_overrides")
    @mock.patch("uaclient.apt.update_sources_list")
    @mock.patch("uaclient.apt.run_apt_install_command")
    @mock.patch("uaclient.apt.run_apt_update_command")
    @mock.patch("uaclient.system.which", return_value=None)
    @mock.patch(M_PATH + "LivepatchEntitlement.application_status")
    @mock.patch(
        M_PATH + "LivepatchEntitlement.can_enable", return_value=(True, None)
    )
    def test_enable_continues_when_snap_install_snapd_fails(
        self,
        m_can_enable,
        m_app_status,
        m_which,
        m_run_apt_update,
        m_run_apt_install,
        m_update_sources_list,
        _m_contract_overrides,
        m_subp,
        _m_get_release_info,
        m_livepatch_proxy,
        m_snap_proxy,
        m_validate_proxy,
        m_is_snapd_installed,
        m_is_snapd_installed_as_a_snap,
        capsys,
        caplog_text,
        event,
        entitlement,
    ):
        """Install snapd and canonical-livepatch snap when not on system."""
        application_status = ApplicationStatus.ENABLED
        m_app_status.return_value = application_status, "enabled"
        m_is_snapd_installed.return_value = False
        m_is_snapd_installed_as_a_snap.return_value = False
        m_subp.side_effect = [
            None,
            exceptions.ProcessExecutionError("test"),
            None,
            None,
            None,
            None,
            None,
            None,
        ]

        progress_mock = mock.MagicMock()
        assert entitlement.enable(progress_mock)
        assert self.mocks_install + self.mocks_config in m_subp.call_args_list
        assert self.mocks_apt_update == m_run_apt_update.call_args_list
        assert 1 == m_update_sources_list.call_count
        assert [
            mock.call("Installing Livepatch"),
            mock.call("Setting up Livepatch"),
        ] == progress_mock.progress.call_args_list
        assert [
            mock.call("message_operation", None),
            mock.call("message_operation", None),
            mock.call("info", "Installing snapd"),
            mock.call("info", "Installing snapd snap"),
            mock.call("info", "Executing `snap install snapd` failed."),
            mock.call("info", "Installing canonical-livepatch snap"),
            mock.call(
                "info", "Disabling Livepatch prior to re-attach with new token"
            ),
        ] == progress_mock.emit.call_args_list
        assert [mock.call(livepatch.LIVEPATCH_CMD)] == m_which.call_args_list
        assert m_validate_proxy.call_count == 2
        assert m_snap_proxy.call_count == 1
        assert m_livepatch_proxy.call_count == 1

    @mock.patch("uaclient.system.get_release_info")
    @mock.patch("uaclient.system.subp")
    @mock.patch("uaclient.contract.apply_contract_overrides")
    @mock.patch("uaclient.system.which", return_value=None)
    @mock.patch(M_PATH + "LivepatchEntitlement.application_status")
    @mock.patch(
        M_PATH + "LivepatchEntitlement.can_enable", return_value=(True, None)
    )
    def test_enable_installs_only_livepatch_snap_when_absent_but_snapd_present(
        self,
        _m_can_enable,
        m_app_status,
        m_which,
        _m_contract_overrides,
        m_subp,
        _m_get_release_info,
        m_livepatch_proxy,
        m_snap_proxy,
        m_validate_proxy,
        m_is_snapd_installed,
        m_is_snapd_installed_as_a_snap,
        capsys,
        event,
        entitlement,
    ):
        """Install canonical-livepatch snap when not present on the system."""
        application_status = ApplicationStatus.ENABLED
        m_app_status.return_value = application_status, "enabled"
        m_is_snapd_installed.return_value = True

        progress_mock = mock.MagicMock()
        assert entitlement.enable(progress_mock)
        assert (
            self.mocks_snap_wait_seed
            + self.mocks_snapd_refresh
            + self.mocks_livepatch_install
            + self.mocks_config
            in m_subp.call_args_list
        )
        assert [
            mock.call("Installing Livepatch"),
            mock.call("Setting up Livepatch"),
        ] == progress_mock.progress.call_args_list
        assert [
            mock.call("message_operation", None),
            mock.call("message_operation", None),
            mock.call("info", "Installing canonical-livepatch snap"),
            mock.call(
                "info", "Disabling Livepatch prior to re-attach with new token"
            ),
        ] == progress_mock.emit.call_args_list
        assert [mock.call(livepatch.LIVEPATCH_CMD)] == m_which.call_args_list
        assert m_validate_proxy.call_count == 2
        assert m_snap_proxy.call_count == 1
        assert m_livepatch_proxy.call_count == 1

    @mock.patch("uaclient.system.get_release_info")
    @mock.patch("uaclient.system.subp")
    @mock.patch("uaclient.contract.apply_contract_overrides")
    @mock.patch(
        "uaclient.system.which", side_effect=["/path/to/exe", "/path/to/exe"]
    )
    @mock.patch(M_PATH + "LivepatchEntitlement.application_status")
    @mock.patch(
        M_PATH + "LivepatchEntitlement.can_enable", return_value=(True, None)
    )
    def test_enable_does_not_install_livepatch_snap_when_present(
        self,
        m_can_enable,
        m_app_status,
        m_which,
        _m_contract_overrides,
        m_subp,
        _m_get_release_info,
        m_livepatch_proxy,
        m_snap_proxy,
        m_validate_proxy,
        m_is_snapd_installed,
        m_is_snapd_installed_as_a_snap,
        capsys,
        event,
        entitlement,
    ):
        """Do not attempt to install livepatch snap when it is present."""
        application_status = ApplicationStatus.ENABLED
        m_app_status.return_value = application_status, "enabled"
        m_is_snapd_installed.return_value = True

        progress_mock = mock.MagicMock()
        assert entitlement.enable(progress_mock)
        subp_calls = [
            mock.call(
                [SNAP_CMD, "wait", "system", "seed.loaded"], capture=True
            ),
            mock.call([SNAP_CMD, "refresh", "snapd"], capture=True),
            mock.call(
                [
                    livepatch.LIVEPATCH_CMD,
                    "config",
                    "remote-server=https://alt.livepatch.com",
                ],
                capture=True,
            ),
            mock.call([livepatch.LIVEPATCH_CMD, "disable"]),
            mock.call(
                [livepatch.LIVEPATCH_CMD, "enable", "livepatch-token"],
                capture=True,
            ),
        ]
        assert subp_calls == m_subp.call_args_list
        assert [
            mock.call("message_operation", None),
            mock.call("message_operation", None),
            mock.call(
                "info", "Disabling Livepatch prior to re-attach with new token"
            ),
        ] == progress_mock.emit.call_args_list
        assert m_validate_proxy.call_count == 2
        assert m_snap_proxy.call_count == 1
        assert m_livepatch_proxy.call_count == 1

    @mock.patch("uaclient.system.get_release_info")
    @mock.patch("uaclient.system.subp")
    @mock.patch("uaclient.contract.apply_contract_overrides")
    @mock.patch(
        "uaclient.system.which", side_effect=["/path/to/exe", "/path/to/exe"]
    )
    @mock.patch(M_PATH + "LivepatchEntitlement.application_status")
    @mock.patch(
        M_PATH + "LivepatchEntitlement.can_enable", return_value=(True, None)
    )
    def test_enable_does_not_disable_inactive_livepatch_snap_when_present(
        self,
        m_can_enable,
        m_app_status,
        m_which,
        _m_contract_overrides,
        m_subp,
        _m_get_release_info,
        m_livepatch_proxy,
        m_snap_proxy,
        m_validate_proxy,
        m_is_snapd_installed,
        m_is_snapd_installed_as_a_snap,
        capsys,
        entitlement,
    ):
        """Do not attempt to disable livepatch snap when it is inactive."""
        m_app_status.return_value = ApplicationStatus.DISABLED, "nope"
        m_is_snapd_installed.return_value = True

        assert entitlement.enable(mock.MagicMock())
        subp_no_livepatch_disable = [
            mock.call(
                [SNAP_CMD, "wait", "system", "seed.loaded"], capture=True
            ),
            mock.call([SNAP_CMD, "refresh", "snapd"], capture=True),
            mock.call(
                [
                    livepatch.LIVEPATCH_CMD,
                    "config",
                    "remote-server=https://alt.livepatch.com",
                ],
                capture=True,
            ),
            mock.call(
                [livepatch.LIVEPATCH_CMD, "enable", "livepatch-token"],
                capture=True,
            ),
        ]
        assert subp_no_livepatch_disable == m_subp.call_args_list
        assert m_validate_proxy.call_count == 2
        assert m_snap_proxy.call_count == 1
        assert m_livepatch_proxy.call_count == 1

    @pytest.mark.parametrize("caplog_text", [logging.WARN], indirect=True)
    @mock.patch("uaclient.system.which", return_value=None)
    @mock.patch("uaclient.system.subp")
    def test_enable_alerts_user_that_snapd_does_not_wait_command(
        self,
        m_subp,
        _m_which,
        m_livepatch_proxy,
        m_snap_proxy,
        m_validate_proxy,
        m_is_snapd_installed,
        m_is_snapd_installed_as_a_snap,
        entitlement,
        capsys,
        caplog_text,
        event,
    ):
        m_is_snapd_installed.return_value = True

        stderr_msg = (
            "error: Unknown command `wait'. Please specify one command of: "
            "abort, ack, buy, change, changes, connect, create-user, disable,"
            " disconnect, download, enable, find, help, install, interfaces, "
            "known, list, login, logout, refresh, remove, run or try"
        )

        m_subp.side_effect = [
            exceptions.ProcessExecutionError(
                cmd="snapd wait system seed.loaded",
                exit_code=-1,
                stdout="",
                stderr=stderr_msg,
            ),
            True,
            True,
        ]

        progress_mock = mock.MagicMock()

        with mock.patch.object(entitlement, "can_enable") as m_can_enable:
            m_can_enable.return_value = (True, None)
            with mock.patch.object(
                entitlement, "setup_livepatch_config"
            ) as m_setup_livepatch:
                entitlement.enable(progress_mock)

                assert 1 == m_can_enable.call_count
                assert 1 == m_setup_livepatch.call_count

        assert (
            mock.call("info", "Installing canonical-livepatch snap")
            in progress_mock.emit.call_args_list
        )

        assert (
            "Detected version of snapd that does not have wait command"
            in caplog_text()
        )

        assert m_validate_proxy.call_count == 2
        assert m_snap_proxy.call_count == 1
        assert m_livepatch_proxy.call_count == 1

    @mock.patch("uaclient.snap.apt.run_apt_update_command")
    @mock.patch("uaclient.system.which", return_value=True)
    @mock.patch("uaclient.system.subp")
    def test_enable_raise_exception_when_snapd_cant_be_installed(
        self,
        m_subp,
        _m_which,
        _m_apt_update,
        m_livepatch_proxy,
        m_snap_proxy,
        m_validate_proxy,
        m_is_snapd_installed,
        m_is_snapd_installed_as_a_snap,
        entitlement,
    ):
        m_is_snapd_installed.return_value = False
        m_subp.side_effect = exceptions.ProcessExecutionError(
            cmd="apt-get install --assume-yes snapd",
            exit_code=-1,
        )

        with mock.patch.object(entitlement, "can_enable") as m_can_enable:
            m_can_enable.return_value = (True, None)
            with mock.patch.object(
                entitlement, "setup_livepatch_config"
            ) as m_setup_livepatch:
                with pytest.raises(exceptions.CannotInstallSnapdError):
                    entitlement.enable(mock.MagicMock())

            assert 1 == m_can_enable.call_count
            assert 0 == m_setup_livepatch.call_count

        assert m_validate_proxy.call_count == 0
        assert m_snap_proxy.call_count == 0
        assert m_livepatch_proxy.call_count == 0

    @mock.patch("uaclient.snap.apt.run_apt_update_command")
    @mock.patch("uaclient.system.which", return_value="/path/to/exe")
    @mock.patch("uaclient.system.subp")
    def test_enable_raise_exception_for_unexpected_error_on_snapd_wait(
        self,
        m_subp,
        _m_which,
        _m_apt_update,
        m_livepatch_proxy,
        m_snap_proxy,
        m_validate_proxy,
        m_is_snapd_installed,
        m_is_snapd_installed_as_a_snap,
        entitlement,
    ):
        m_is_snapd_installed.return_value = True
        stderr_msg = "test error"

        m_subp.side_effect = exceptions.ProcessExecutionError(
            cmd="snapd wait system seed.loaded",
            exit_code=-1,
            stdout="",
            stderr=stderr_msg,
        )

        with mock.patch.object(entitlement, "can_enable") as m_can_enable:
            m_can_enable.return_value = (True, None)
            with mock.patch.object(
                entitlement, "setup_livepatch_config"
            ) as m_setup_livepatch:
                with pytest.raises(
                    exceptions.ProcessExecutionError
                ) as excinfo:
                    entitlement.enable(mock.MagicMock())

            assert 1 == m_can_enable.call_count
            assert 0 == m_setup_livepatch.call_count

        expected_msg = "test error"
        assert expected_msg in str(excinfo)
        assert m_validate_proxy.call_count == 0
        assert m_snap_proxy.call_count == 0
        assert m_livepatch_proxy.call_count == 0


class TestLivepatchApplicationStatus:
    @pytest.mark.parametrize("which_result", (("/path/to/exe"), (None)))
    @pytest.mark.parametrize(
        "livepatch_status_result",
        (
            (None),
            (
                livepatch.LivepatchStatusStatus(
                    kernel=None, livepatch=None, supported=None
                )
            ),
        ),
    )
    @mock.patch("uaclient.system.which")
    @mock.patch("uaclient.livepatch.status")
    def test_application_status(
        self,
        m_livepatch_status,
        m_which,
        livepatch_status_result,
        which_result,
        entitlement,
    ):
        m_which.return_value = which_result
        m_livepatch_status.return_value = livepatch_status_result

        status, details = entitlement.application_status()

        if not which_result:
            assert status == ApplicationStatus.DISABLED
            assert "canonical-livepatch snap is not installed." in details.msg
        elif livepatch_status_result is None:
            assert status == ApplicationStatus.DISABLED
            assert (
                messages.LIVEPATCH_APPLICATION_STATUS_CLIENT_FAILURE == details
            )
        else:
            assert status == ApplicationStatus.ENABLED
            assert details is None

    @mock.patch("uaclient.livepatch.is_livepatch_installed", return_value=True)
    @mock.patch("uaclient.livepatch.status")
    def test_application_status_when_canonical_livepatch_fails(
        self, m_status, _m_livepatch_installed, entitlement
    ):
        m_status.side_effect = exceptions.ProcessExecutionError(
            cmd="test", stdout="", stderr="livepatch error"
        )

        status, details = entitlement.application_status()

        assert status == ApplicationStatus.WARNING
        assert details == messages.LIVEPATCH_CLIENT_FAILURE_WARNING.format(
            livepatch_error="livepatch error"
        )

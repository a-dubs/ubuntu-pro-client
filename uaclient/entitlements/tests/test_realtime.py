import mock
import pytest

from uaclient import messages
from uaclient.entitlements.entitlement_status import ApplicabilityStatus
from uaclient.entitlements.realtime import (
    IntelIotgRealtime,
    RaspberryPiRealtime,
)
from uaclient.system import CpuInfo

M_PATH = "uaclient.entitlements.realtime."


class TestRaspiVariant:
    @pytest.mark.parametrize(
        [
            "load_file_side_effect",
            "expected_result",
        ],
        [
            (["Raspberry Pi 5 Model B Rev 1.0"], True),
            (["Raspberry Pi 4 Model B Rev 1.1"], True),
            (["Raspberry Pi 3 Model B Plus Rev 1.3"], False),
            ([FileNotFoundError()], False),
        ],
    )
    @mock.patch(M_PATH + "system.load_file")
    def test_variant_auto_select(
        self,
        m_load_file,
        load_file_side_effect,
        expected_result,
        FakeConfig,
    ):
        m_load_file.side_effect = load_file_side_effect
        assert (
            expected_result
            == RaspberryPiRealtime(FakeConfig()).variant_auto_select()
        )


class TestIntelIOTGVariannt:
    @pytest.mark.parametrize(
        "cpu_info,expected_status,expected_msg",
        (
            (
                CpuInfo(vendor_id="test", model=None, stepping=None),
                ApplicabilityStatus.INAPPLICABLE,
                messages.INAPPLICABLE_VENDOR_NAME.format(
                    title=IntelIotgRealtime.title,
                    vendor="test",
                    supported_vendors="intel",
                ),
            ),
            (
                CpuInfo(vendor_id="intel", model=None, stepping=None),
                ApplicabilityStatus.APPLICABLE,
                None,
            ),
        ),
    )
    @mock.patch("uaclient.system.get_kernel_info")
    @mock.patch("uaclient.system.get_cpu_info")
    def test_applicability_status(
        self,
        m_get_cpu_info,
        _m_get_kernel_info,
        cpu_info,
        expected_status,
        expected_msg,
        FakeConfig,
    ):
        m_get_cpu_info.return_value = cpu_info
        ent = IntelIotgRealtime(FakeConfig())
        with mock.patch.object(
            ent, "_base_entitlement_cfg"
        ) as m_entitlement_cfg:
            m_entitlement_cfg.return_value = {
                "entitlement": {
                    "affordances": {
                        "platformChecks": {
                            "cpu_vendor_ids": ["intel"],
                        }
                    }
                }
            }
            actual_ret = ent.applicability_status()
            assert (
                expected_status,
                expected_msg,
            ) == actual_ret

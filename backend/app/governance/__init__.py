"""治理层 —— 让 Agent 的"破坏性能力"始终有守门人

分层职责：

    contracts/   工具契约（能力边界写在这里）
    tools/       工具实现（只做事，不判断权限）
    governance/  本层 —— 判断能不能做
    engine/      循环编排
    services/    原子能力

`tools` 层不许自己判断「能不能生成图」，一律问本层。
理由很简单：判断逻辑一旦有两份实现，迟早有一份会漏。
"""
from governance.guard import (  # noqa: F401
    DISABLE_IMAGE_GENERATION,
    GovernanceError,
    assert_safe_image,
    check_generation_allowed,
    check_upload_size,
    policy_snapshot,
    release_generation,
    remaining_quota,
    reserve_generation,
    reset_quota,
    settle_generation,
)

__all__ = [
    "GovernanceError",
    "assert_safe_image",
    "check_generation_allowed",
    "check_upload_size",
    "policy_snapshot",
    "release_generation",
    "remaining_quota",
    "reserve_generation",
    "reset_quota",
    "settle_generation",
]

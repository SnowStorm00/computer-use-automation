from cua.artifact.schema import Checkpoint, ExceptionRule, FrameRef, LocatorCandidate, Target


WORKSPACE = [FrameRef(by="name", value="workspace")]


def default_exception_rules() -> list[ExceptionRule]:
    return [
        ExceptionRule(
            code="MEMBER_NOT_FOUND",
            class_="business_outcome",
            detect=Checkpoint(kind="text_visible", text="No member found"),
            action="return",
            message="No member matched the given ID.",
        ),
        ExceptionRule(
            code="ACCESS_DENIED",
            class_="business_outcome",
            detect=Checkpoint(kind="text_visible", text="Access denied"),
            action="return",
            message="Operator is not permitted to view this member.",
        ),
        ExceptionRule(
            code="VALIDATION_ERROR",
            class_="business_outcome",
            detect=Checkpoint(kind="text_visible", text="Member ID is required"),
            action="return",
            message="The form was submitted without a member ID.",
        ),
        ExceptionRule(
            code="SESSION_EXPIRED",
            class_="hard_failure",
            detect=Checkpoint(kind="text_visible", text="Session expired"),
            action="fail",
            message="The CoreLink session expired.",
        ),
        ExceptionRule(
            code="SIGN_ON_FAILED",
            class_="hard_failure",
            detect=Checkpoint(kind="text_visible", text="Sign-on failed"),
            action="fail",
            message="Operator credentials were rejected.",
        ),
        ExceptionRule(
            code="PROCESSING_HOLD",
            class_="recoverable",
            detect=Checkpoint(kind="text_visible", text="Core processing hold"),
            action="escalate",
            message="Unexpected core processing hold dialog.",
        ),
    ]


def workspace_target(description: str, locators: list[LocatorCandidate]) -> Target:
    return Target(description=description, frames=WORKSPACE, locators=locators)

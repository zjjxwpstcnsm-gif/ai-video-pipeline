REQUIRED_LEDGER_FIELDS = [
    "shot_id", "duration", "subject_identity", "subject_position", "camera_motion",
    "motion_direction", "start_state", "main_action", "payoff", "exit_type",
    "exit_direction", "next_entry_state",
]


def validate_ledger_entry(entry: dict) -> list[str]:
    return [f"missing continuity field: {field}" for field in REQUIRED_LEDGER_FIELDS if field not in entry]

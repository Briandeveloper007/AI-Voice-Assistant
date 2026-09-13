
class SafetyEngine:
    PROTECTED_PATHS = ["C:\\Windows", "C:\\Program Files", "C:\\ProgramData"]

    def evaluate(self, intent, current_permission: str) -> str:
        if intent.application == "filesystem":
            target_path = intent.parameters.get("path", "").upper()
            for protected in self.PROTECTED_PATHS:
                if target_path.startswith(protected.upper()):
                    return "BLOCK"
        
        if intent.action == "format_drive":
            return "BLOCK"

        return current_permission

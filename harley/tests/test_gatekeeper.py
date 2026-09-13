import pytest
from pydantic import ValidationError

from commands.schema import StructuredIntent, IntentOrigin
# from security.permissions import PermissionManager
# from security.safety import SafetyEngine

# =====================================================================
# MOCK CLASSES (For demonstration - replace with actual imports)
# =====================================================================

class PermissionManager:
    """Mock Permission Manager evaluating the 4-level matrix."""
    def __init__(self):
        # Simulated database of permissions using UPPER_SNAKE_CASE
        self.matrix = {
            "spotify.PLAY_MUSIC": "ALLOW",
            "spotify.DELETE_PLAYLIST": "CONFIRM",
            "gmail.READ_EMAIL": "ALLOW",
            "gmail.SEND_EMAIL": "CONFIRM",
            "filesystem.DELETE_FILE": "CONFIRM",
            "windows.FORMAT_DRIVE": "DENY"
        }

    def evaluate(self, intent: StructuredIntent) -> str:
        key = f"{intent.application}.{intent.action}"
        base_permission = self.matrix.get(key, "DENY") 

        # TAINT ANALYSIS: Downgrade ALLOW to CONFIRM for untrusted origins
        if intent.origin == IntentOrigin.INDIRECT_WEB_CONTENT and base_permission == "ALLOW":
            return "CONFIRM"
            
        return base_permission

class SafetyEngine:
    """Mock Safety Engine enforcing immutable system invariants."""
    PROTECTED_PATHS = ["C:\\Windows", "C:\\Program Files", "C:\\ProgramData"]

    def evaluate(self, intent: StructuredIntent, current_permission: str) -> str:
        if intent.application == "filesystem":
            target_path = intent.parameters.get("path", "").upper()
            for protected in self.PROTECTED_PATHS:
                if target_path.startswith(protected.upper()):
                    return "BLOCK"
        
        if intent.action == "FORMAT_DRIVE":
            return "BLOCK"

        return current_permission

# =====================================================================
# PYTEST SUITE
# =====================================================================

@pytest.fixture
def permission_manager():
    return PermissionManager()

@pytest.fixture
def safety_engine():
    return SafetyEngine()

def run_dry_run_pipeline(intent: StructuredIntent, perm_mgr: PermissionManager, safety_eng: SafetyEngine) -> str:
    permission_level = perm_mgr.evaluate(intent)
    final_level = safety_eng.evaluate(intent, permission_level)
    return final_level


# 1. TEST THE BASE PERMISSION MATRIX
@pytest.mark.parametrize("application, action, expected_level", [
    ("spotify", "PLAY_MUSIC", "ALLOW"),
    ("spotify", "DELETE_PLAYLIST", "CONFIRM"),
    ("gmail", "READ_EMAIL", "ALLOW"),
    ("gmail", "SEND_EMAIL", "CONFIRM"),
    ("unknown_app", "DO_SOMETHING", "DENY"), 
    ("windows", "FORMAT_DRIVE", "BLOCK")     
])
def test_standard_permissions(permission_manager, safety_engine, application, action, expected_level):
    intent = StructuredIntent(
        intent="test_intent",
        application=application,
        action=action,
        parameters={},
        origin=IntentOrigin.DIRECT_USER
    )
    result = run_dry_run_pipeline(intent, permission_manager, safety_engine)
    assert result == expected_level


# 2. TEST TAINT ANALYSIS (PROMPT INJECTION DEFENSE)
@pytest.mark.parametrize("application, action, origin, expected_level", [
    ("spotify", "PLAY_MUSIC", IntentOrigin.DIRECT_USER, "ALLOW"),
    ("gmail", "READ_EMAIL", IntentOrigin.DIRECT_USER, "ALLOW"),
    
    ("spotify", "PLAY_MUSIC", IntentOrigin.INDIRECT_WEB_CONTENT, "CONFIRM"),
    ("gmail", "READ_EMAIL", IntentOrigin.INDIRECT_WEB_CONTENT, "CONFIRM"),
    
    ("gmail", "SEND_EMAIL", IntentOrigin.INDIRECT_WEB_CONTENT, "CONFIRM"),
])
def test_taint_analysis_downgrades(permission_manager, safety_engine, application, action, origin, expected_level):
    intent = StructuredIntent(
        intent="test_intent",
        application=application,
        action=action,
        parameters={},
        origin=origin
    )
    result = run_dry_run_pipeline(intent, permission_manager, safety_engine)
    assert result == expected_level


# 3. TEST SAFETY ENGINE (PATH BLACKLISTS)
@pytest.mark.parametrize("path, expected_level", [
    ("D:\\User\\Documents\\Resume.pdf", "CONFIRM"),       
    ("C:\\Temp\\junk.txt", "CONFIRM"),                    
    ("C:\\Windows\\System32\\cmd.exe", "BLOCK"),          
    ("C:\\Program Files\\Spotify\\spotify.exe", "BLOCK"), 
    ("c:\\windows\\syswow64", "BLOCK"),                   
])
def test_safety_engine_protected_paths(permission_manager, safety_engine, path, expected_level):
    intent = StructuredIntent(
        intent="delete_file",
        application="filesystem",
        action="DELETE_FILE",
        parameters={"path": path},
        origin=IntentOrigin.DIRECT_USER
    )
    result = run_dry_run_pipeline(intent, permission_manager, safety_engine)
    assert result == expected_level


# 4. TEST MALICIOUS CHAINING (SAFETY TRUMPS ALL)
def test_safety_overrides_tainted_allow(permission_manager, safety_engine):
    malicious_intent = StructuredIntent(
        intent="destroy_system",
        application="filesystem",
        action="DELETE_FILE",
        parameters={"path": "C:\\Windows\\explorer.exe"},
        origin=IntentOrigin.INDIRECT_WEB_CONTENT 
    )
    
    perm_result = permission_manager.evaluate(malicious_intent)
    assert perm_result == "CONFIRM"
    
    final_result = safety_engine.evaluate(malicious_intent, perm_result)
    assert final_result == "BLOCK"
    
# 5. TEST SCHEMA VALIDATORS
def test_schema_validators():
    # Test confidence bounds
    with pytest.raises(ValidationError):
        StructuredIntent(intent="test", action="TEST_ACTION", confidence=1.5)
        
    # Test action case
    with pytest.raises(ValidationError):
        StructuredIntent(intent="test", action="test_action")
        
    # Test extra fields
    with pytest.raises(ValidationError):
        StructuredIntent.model_validate(
            {"intent": "test", "action": "TEST_ACTION", "hallucinated_field": "bad"}
        )
        
    # Test origin conversion
    intent = StructuredIntent.model_validate({
        "intent": "test", 
        "action": "TEST", 
        "origin": "CHROME_EXTENSION"
    })
    assert intent.origin == IntentOrigin.INDIRECT_WEB_CONTENT

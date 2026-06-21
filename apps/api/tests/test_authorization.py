from app.core.authorization import ROLE_HIERARCHY


class TestRoleHierarchy:
    def test_viewer_is_lowest(self):
        assert ROLE_HIERARCHY["viewer"] < ROLE_HIERARCHY["editor"]
        assert ROLE_HIERARCHY["viewer"] < ROLE_HIERARCHY["admin"]

    def test_editor_below_admin(self):
        assert ROLE_HIERARCHY["editor"] < ROLE_HIERARCHY["admin"]

    def test_viewer_cannot_satisfy_editor_requirement(self):
        viewer_level = ROLE_HIERARCHY.get("viewer", -1)
        editor_required = ROLE_HIERARCHY.get("editor", 0)
        assert viewer_level < editor_required

    def test_admin_satisfies_all_requirements(self):
        admin_level = ROLE_HIERARCHY.get("admin", -1)
        for role in ("viewer", "editor", "admin"):
            assert admin_level >= ROLE_HIERARCHY.get(role, 0)

    def test_unknown_role_defaults_to_unauthorized(self):
        # get() with default -1 simulates how require_role() handles
        # a malformed/unknown role string from a corrupted DB row
        unknown_level = ROLE_HIERARCHY.get("nonexistent_role", -1)
        assert unknown_level < ROLE_HIERARCHY.get("viewer", 0)
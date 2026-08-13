from .bases import ALttPRTestBaseNoDefaultTests


class TestKeyDropOverridesNoPottery(ALttPRTestBaseNoDefaultTests):
    options = {
        "key_drop_shuffle": True,
        "pot_shuffle": "none",
    }

    def test_key_drop_overrides_no_pottery(self):
        assert self.world.door_rando_world.pottery[1] == "keys"


class TestKeyDropOverridesCavePottery(ALttPRTestBaseNoDefaultTests):
    options = {
        "key_drop_shuffle": True,
        "pot_shuffle": "cave",
    }

    def test_key_drop_overrides_cave_pottery(self):
        assert self.world.door_rando_world.pottery[1] == "cavekeys"

class TestCavePotteryWithoutKeyDrop(ALttPRTestBaseNoDefaultTests):
    options = {
        "pot_shuffle": "cave",
    }

    def test_cave_pottery_without_key_drop(self):
        assert self.world.door_rando_world.pottery[1] == "cave"

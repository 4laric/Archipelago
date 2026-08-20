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


class TestKeyDropAndNoSwordWithNoEnemyDropShuffle(ALttPRTestBaseNoDefaultTests):
    options = {
        "key_drop_shuffle": True,
        "enemy_drop_shuffle": "none",
    }

    def test_key_drop_overrides_no_enemy_drop(self):
        assert self.world.door_rando_world.dropshuffle[1] == "keys"
        assert self.world.door_rando_world.precollected_items != ["Progressive Sword"]
        assert self.world.door_rando_world.dungeon_counters[1] == "pickup"


class TestEnemyDropShuffleUnderworld(ALttPRTestBaseNoDefaultTests):
    options = {
        "enemy_drop_shuffle": "underworld",
    }

    def test_enemy_drop_shuffle_underworld(self):
        assert self.world.door_rando_world.dropshuffle[1] == "underworld"
        assert self.world.door_rando_world.precollected_items == ["Progressive Sword"]
        assert self.world.door_rando_world.dungeon_counters[1] == "on"

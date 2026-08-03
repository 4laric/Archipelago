from bases import ALttPRTestBase, ALttPRTestBaseNoDefaultTests


class TestRandomSettings(ALttPRTestBase):
    # Just making sure that every setting can be randomized.
    # Several settings have a value which gets converted to the string
    # "random", so I'm a bit paranoid.
    options = {
        "world_mode": "random",
        "goal": "random",
        "open_pyramid": "random",
        "crystals_needed_for_ganons_tower": "random",
        "crystals_needed_for_ganon": "random",
        "triforce_hunt_goal": "random-range-1-30",
        "triforce_hunt_total": "random-range-30-50",
        "map_shuffle": "random",
        "compass_shuffle": "random",
        "small_key_shuffle": "random",
        "big_key_shuffle": "random",
        "key_drop_shuffle": "random",
        "entrance_shuffle": "random",
        "shuffle_links_house": "random",
        "shuffle_tavern": "random",
        "zelgawoods": "random",
        "door_shuffle": "random",
        "door_type_shuffle": "random",
        "lobby_shuffle": "random",
        "enemy_shuffle": "random",
        "boss_shuffle": "random",
        "shopsanity": "random",
        "prize_shuffle": "random",
        "flute_shuffle": "random",
        "pre_activated_flute": "random",
        "pseudoboots": "random",
        "mirror_scroll": "random",
        "heart_beep_rate": "random",
        "heart_color": "random",
        "fast_menu": "random",
        "disable_music": "random",
        "msu_resume": "random",
    }
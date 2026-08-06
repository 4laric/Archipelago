from test.bases import WorldTestBase


class ALttPRTestBase(WorldTestBase):
    game = "The Legend of Zelda: A Link to the Past"

    def assertCanReachWith(self, areas: list[str], area_type: str, item_combinations: list[list[str]]):
        if area_type == "entrance":
            can_reach = self.can_reach_entrance
        elif area_type == "location":
            can_reach = self.can_reach_location
        elif area_type == "region":
            can_reach = self.can_reach_region
        else:
            assert False, f"Unknown area type {area_type}"

        for area in areas:
            assert not can_reach(area), f"Can already reach {area_type} {area} without any of the required items."

        for item_combination in item_combinations:
            items = [self.get_item_by_name(item) for item in item_combination]
            for item in items:
                self.collect(item)

            for area in areas:
                assert can_reach(area), f"Could not reach {area_type} {area} despite having the items {item_combination}."

            for item in items:
                self.remove(item)


    def assertCanNotReachWith(self, areas: list[str], area_type: str, item_combinations: list[list[str]]):
        if area_type == "entrance":
            can_reach = self.can_reach_entrance
        elif area_type == "location":
            can_reach = self.can_reach_location
        elif area_type == "region":
            can_reach = self.can_reach_region
        else:
            assert False, f"Unknown area type {area_type}"

        for area in areas:
            assert not can_reach(area), f"Can already reach {area_type} {area} without any of the required items."

        for item_combination in item_combinations:
            items = [self.get_item_by_name(item) for item in item_combination]
            for item in items:
                self.collect(item)

            for area in areas:
                assert not can_reach(area), f"Could reach {area_type} {area} with the items {item_combination}."

            for item in items:
                self.remove(item)


class ALttPRTestBaseNoDefaultTests(ALttPRTestBase):
    run_default_tests = False
from typing import Dict, List
from BaseClasses import ItemClassification, MultiWorld, Region
from worlds.AutoWorld import World
from .Items import ArmoredCore6Item, items
from .Locations import ArmoredCore6Location

# TODO: what is a webworld and do I need one?

class ArmoredCore6World(World):
    """A FromSoft Mech game"""
    game = "Armored Core 6"
    option_definitions = {}
    # TODO: topology present?

    # TODO
    item_name_to_id: Dict[str, int] = {}
    location_name_to_id: Dict[str, int] = {}

    start_id = 2023_08_25

    data_version = 1


    base_id = 2023_08_25

    def __init__(self, multiworld: MultiWorld, player: int):
        super().__init__(multiworld, player)

    def generate_early(self):
        # TODO
        pass

    def create_items(self):
        for item in items:
            self.multiworld.itempool.append(self._create_item(item))

    def _create_item(self, name: str):
        # TODO: second parameter
        # TODO: item classification for non-progression
        return ArmoredCore6Item(name, ItemClassification.progression, self.item_name_to_id[name], self.player)

    def create_regions(self):
        # TODO: source from locations file
        self.multiworld.regions += [
            self._create_region("Tutorial", ["Illegal Entry"]),
        ];

        # TODO: make connections?

    def _create_region(self, name: str, location_names: List[str]):
        region = Region(name, self.player, self.multiworld)
        for location_name in location_names:
            # TODO: other options?
            location = ArmoredCore6Location(self.player, location_name)
            region.locations.append(location)
        # TODO: exits?
        return region

    def set_rules(self):
        # TODO
        pass

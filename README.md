# The Legend of Zelda: A Link to the Past

This is a new APWorld for The Legend of Zelda: A Link to the Past, built on top of the standalone Overworld Randomizer (OWR). This is the most up-to-date randomizer with many new options and QoL improvements which haven't been ported to AP before.

## Player installation
The setup for this APWorld is largely identical to the [setup guide for the existing ALttP APWorld](https://archipelago.gg/tutorial/A%20Link%20to%20the%20Past/multiworld_en), with a few small differences:
* The .apworld file from the latest release must be installed in your custom_worlds folder
* Your .yaml file must be based on the template file from the latest release
* The person generating the multiworld does not need a ROM file, only the players do
* The resulting patch for each player will have the extension .apalttpr

## Features
* Goal options: Crystals, Ganon, All Dungeons, Pedestal, Triforce Hunt, Ganon Hunt, Trinity (complete any one of Ganon/Pedestal/Triforce Hunt goals), Completionist (collect every check and then kill Ganon),
* Standard and Inverted game modes,
* Set the number of crystals required for GT/Ganon, and number of Triforce pieces,
* All varieties of keysanity, including key drop shuffle,
* Shopsanity
* Potsanity
* Enemy drops are checks
* Enemy and boss shuffle, including logical enemy shuffle where enemies which require specific items to kill can block progression,
* Start without bombs
* Crossed entrance shuffle
* Option for Zelgawoods to add two Skull Woods dropdowns into the entrance shuffle pool. Example: https://raw.githubusercontent.com/aurabot24/Archipelago-ALttPR/refs/heads/alttpr/worlds/alttpr/docs/Zelgawoods.png,
* Random dungeon layouts
* Option to add Pendants and Crystals to the item pool
* Randomize flute spots and/or automatically activate the Flute upon pickup
* Options for Pseudoboots (dash from the start, but without any of the progression from Pegasus Boots) and Mirror Scroll (Y item that warps Link to the start of the current dungeon),
* FastROM to significantly reduce in-game lag,
* AP items are visually distinct: progressive items appear as green clocks, useful items as blue clocks, and filler/trap items as red clocks,
* Can be generated without a ROM file
* Universal Tracker support
 
## Known Bugs:
* Rare generation failures or extremely slow generation times, mostly with door rando
* Some checks aren't sent until you leave their room
* Triforce Pieces and Small Keys can't be in the starting inventory,
* The spoiler log shows a lot of events that should be hidden
* Door rando does not work with standard mode, no keysanity, or no key drop
* Universal Tracker does not work with logical enemy shuffle, and has key logic errors or sometimes fails to connect for door rando

## Developer installation:
  1. Clone this repo
  2. Run "git submodule update --init".
  
The code for this APWorld is in worlds/alttpr, with the main ALttPRWorld class in World.py. Almost all of the actual randomizing is done in ALttPDoorRandomizer, a submodule containing an edited copy of the standalone OWR (most of the edits are to make the imports relative so it will compile inside AP). The code written for this APWorld is playing middleman; setting up OWR with the YAML settings, converting OWR items and locations to AP objects, telling OWR where AP has placed each item, and so on.

There is one helper script added; with one ALttP YAML and other optional YAMLs in the Players folder, you can go to "worlds/alttpr" and run "generate_and_launch.py" as a shortcut to delete the output folder, generate a new multiworld, start a local server, and launch ALttP.
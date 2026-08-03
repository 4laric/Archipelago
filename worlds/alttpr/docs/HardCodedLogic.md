This file documents any hard coded logic that I have added on top of door randomizer. This is in progress and not a complete list.

## Bombs and Rupees
It is currently assumed that Bombs Rupees are always available and farmable.

## Standard Start
The keys are manually placed in specific locations with a standard start, to prevent an early BK.

## Follower Logic
All of it. The follower logic in OWR is built into can_reach_entrance, which is a problem because we have to use Archipelago's can_reach_entrance code. Currently this only applies to the Big Bomb, but more logic will be added for Follower Shuffle.

## Crystal Switches
Without door shuffle, the crystal logic is hard coded for only SP, IP, and MM. For door shuffle, a graph traversal method is used to find all crystals.

## Turtle Rock
Extra code was added to OWR so that the front and back of TR cannot both be must-exits when keysanity is disabled, to prevent key logic errors where not enough keys can be placed in the middle to put the front or back in logic.
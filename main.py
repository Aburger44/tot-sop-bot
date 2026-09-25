"""Your strategy goes here.

    python main.py

The bot opens the page and then waits. **You** load the game and pick the mode —
it will not touch anything. The moment the RideControl panel appears on screen the
game has begun, ``wait_for_panel()`` returns, and the loop below starts.

Everything below the setup block is a skeleton. The classes deliberately contain
no decisions: every property is one thing on screen, so every branch you write
here maps to one instruction a person could follow.
"""

from __future__ import annotations

from tot import DEFAULT_URL, Game, RuffleCrashed

# The Simulation Page only offers the Unlimited game. Point this at the TM Arcade
# URL when you want the timed, scored run.
URL = DEFAULT_URL

# --------------------------------------------------------------------------
# What is readable, in full.
#
# game.tv_room1 / game.tv_room2
#   States:  load_button unload_button entrance exit enable_button preshow
#   Booleans: can_load is_loading unload_ready is_unloading
#             entrance_is_closed/_moving/_open  exit_is_closed/_moving/_open
#             enabled preshow_ready preshow_running
#   Counters: waiting loaded                       -> Count.ZERO / FULL / OTHER
#             waiting_is_zero waiting_is_full loaded_is_zero loaded_is_full
#   Clicks:   load() unload() start_preshow() toggle_enabled()
#
# game.elevator1 / elevator2 / elevator3
#   States:   enable_button doors load_button dispatch_button
#   Booleans: enabled doors_are_closed/_moving/_open
#             can_load is_loading can_dispatch is_dispatched
#   Counters: waiting loaded (+ the four booleans, as above)
#   Clicks:   load() dispatch() toggle_enabled() toggle_doors()
#
# game.track       state is_ready is_locked           (read-only)
# game.control     attraction attraction_is_active/_closed
#                  front_waiting back_waiting front_queue_is_full back_queue_is_full
#                  automatic_doors daytime ride_sfx tv_room_sound bgm
#                  toggle_attraction() toggle_automatic_doors() toggle_daytime()
#                  toggle_show_fullscreen() toggle_ride_sfx()
#                  toggle_tv_room_sound() toggle_bgm()
# game             refresh() wait_until(pred, timeout) describe() panel_visible
#
# A counter only ever says ZERO, FULL (21) or OTHER. Those are the two numbers a
# decision turns on; every other value means "wait", and the bot does not spend a
# frame working out which one it is.
#
# All 27 clickable buttons: 8 across the TV rooms, 12 across the elevators,
# 7 on RideControl. Track and every counter are read-only.
# --------------------------------------------------------------------------
START_ELEVATOR_2 = False # to know if elevator 2 can be started
START_ELEVATOR_3 = False # to know if elevator 3 can be started
START_TVRM_2 = False # to know if tvrm 1 has been loaded

def play(game: Game) -> None:
    """One pass of your strategy. Called in a loop, once per frame.

    ``game`` has already been refreshed, so every property below describes the
    same instant. Do not refresh in here.
    """
    global START_ELEVATOR_2, START_ELEVATOR_3, START_TVRM_2
    # Open the attraction, once. This is safe to write as a plain "if it is not
    # open, open it" even though the loop runs 24 times a second: a button is not
    # pressed again until the panel has actually changed in response, so the
    # press below happens once and the rest are suppressed. Every action returns
    # True if it pressed and False if it was suppressed, if you want to know.

    # Start Ride
    if not game.control.attraction_is_active:
        game.control.toggle_attraction()

    """
    ==============
    TV Room Logic
    ==============
    """
    # Enable Rooms
    if not game.tv_room1.enabled:
        game.tv_room1.toggle_enabled()

    if not game.tv_room2.enabled and START_TVRM_2:
        game.tv_room2.toggle_enabled()

    # Load Rooms
    if game.tv_room1.can_load and game.tv_room1.waiting_is_full:
        game.tv_room1.load()
        START_TVRM_2 = True

    if game.tv_room2.can_load and game.tv_room2.waiting_is_full:
        game.tv_room2.load()

    # Preshows
    if game.tv_room1.preshow_ready:
        game.tv_room1.start_preshow()

    if game.tv_room2.preshow_ready:
        game.tv_room2.start_preshow()

    # Unloading
    if game.tv_room1.unload_ready and not game.tv_room1.is_unloading:
        game.tv_room1.unload()
    
    if game.tv_room2.unload_ready and not game.tv_room1.is_unloading:
        game.tv_room2.unload()

    """
    ==============
    Elevators Logic
    ==============
    """
    # Enable Elevators
    if not game.elevator1.enabled:
        game.elevator1.toggle_enabled()

    if not game.elevator2.enabled and START_ELEVATOR_2:
        game.elevator2.toggle_enabled()

    if not game.elevator3.enabled and START_ELEVATOR_3:
        game.elevator3.toggle_enabled()

    # Door Controls
    if game.elevator1.doors_are_closed and not game.elevator1.is_loading and not game.elevator1.loaded_is_full:
        game.elevator1.toggle_doors()

    if game.elevator2.enabled and game.elevator2.doors_are_closed and not game.elevator2.is_loading and not game.elevator2.can_dispatch:
        game.elevator2.toggle_doors()

    if game.elevator3.enabled and game.elevator3.doors_are_closed and not game.elevator3.is_loading and not game.elevator3.loaded_is_full:
        game.elevator3.toggle_doors()

    # Elevator Loading
    if game.elevator1.can_load and game.elevator1.waiting_is_full:
        START_ELEVATOR_2 = True
        game.elevator1.load()

    if game.elevator2.enabled and game.elevator2.can_load and game.elevator2.waiting_is_full:
        START_ELEVATOR_3 = True
        game.elevator2.load()

    if game.elevator3.enabled and game.elevator3.can_load and game.elevator3.waiting_is_full:
        game.elevator3.load()

    # Dispatch Elevator
    if game.elevator1.loaded_is_full and game.track.is_ready:
        game.elevator1.dispatch()

    if game.elevator2.enabled and game.elevator2.can_dispatch and game.track.is_ready:
        game.elevator2.dispatch()

    if game.elevator3.enabled and game.elevator3.loaded_is_full and game.track.is_ready:
        game.elevator3.dispatch()

    return


def main() -> None:
    game = Game.launch(
        url=URL,
        headless=False,
        # "raise" stops the run on a fill we have not seen before. There is no
        # "guess" mode on purpose: a misread colour is a wrong decision.
        on_unknown="raise",
        # 0.0 lets the bot play as fast as it can. Set a floor (say 0.4) to find
        # out what the same strategy scores at human speed.
        min_click_interval=0.0,
        # How long a button may go unanswered before the bot tries pressing it
        # again. Set to 0 to press whenever your rules say to, and own the
        # consequences — see Game._click.
        reclick_after=1.0,
    )

    with game:
        print("Load the game and pick your mode. I start when I can see the panel.")
        if not game.wait_for_panel(timeout=600):
            print("No RideControl panel after 10 minutes — giving up.")
            print(game.backend.describe_page())
            return

        print(f"Panel found ({game.layout.region.w}x{game.layout.region.h} crop). Playing.")
        try:
            while game.refresh():
                play(game)
        except RuffleCrashed as exc:
            print(f"\n{exc}\nReload the page and run again.")
        except KeyboardInterrupt:
            pass

        if not game.panel_visible:
            print("The panel went away — the game is over, or the emulator dropped it.")


if __name__ == "__main__":
    main()

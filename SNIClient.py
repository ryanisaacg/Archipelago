from __future__ import annotations

import sys
import threading
import time
import multiprocessing
import os
import subprocess
import base64
import logging
import asyncio
import enum
import typing

from json import loads, dumps

# CommonClient import first to trigger ModuleUpdater
from CommonClient import CommonContext, server_loop, ClientCommandProcessor, gui_enabled, get_base_parser

import Utils
from worlds.alttp.Client import WRAM_START, SRAM_START, ROMNAME_START, ROMNAME_SIZE, INGAME_MODES, ENDGAME_MODES, \
    DEATH_MODES, SAVEDATA_START, RECV_PROGRESS_ADDR, RECV_ITEM_ADDR, RECV_ITEM_PLAYER_ADDR, SCOUTREPLY_LOCATION_ADDR, \
    SCOUTREPLY_ITEM_ADDR, SCOUTREPLY_PLAYER_ADDR, DEATH_LINK_ACTIVE_ADDR, track_locations, get_alttp_settings

if __name__ == "__main__":
    Utils.init_logging("SNIClient", exception_logger="Client")

import colorama
import websockets

from NetUtils import ClientStatus, color
from worlds.alttp.Rom import ROM_PLAYER_LIMIT
from worlds.sm.Rom import ROM_PLAYER_LIMIT as SM_ROM_PLAYER_LIMIT
from worlds.smz3.Rom import ROM_PLAYER_LIMIT as SMZ3_ROM_PLAYER_LIMIT
from Patch import GAME_ALTTP, GAME_SM, GAME_SMZ3, GAME_DKC3, GAME_SMW


snes_logger = logging.getLogger("SNES")

from MultiServer import mark_raw


class DeathState(enum.IntEnum):
    killing_player = 1
    alive = 2
    dead = 3


class SNIClientCommandProcessor(ClientCommandProcessor):
    ctx: Context

    def _cmd_slow_mode(self, toggle: str = ""):
        """Toggle slow mode, which limits how fast you send / receive items."""
        if toggle:
            self.ctx.slow_mode = toggle.lower() in {"1", "true", "on"}
        else:
            self.ctx.slow_mode = not self.ctx.slow_mode

        self.output(f"Setting slow mode to {self.ctx.slow_mode}")

    @mark_raw
    def _cmd_snes(self, snes_options: str = "") -> bool:
        """Connect to a snes. Optionally include network address of a snes to connect to,
        otherwise show available devices; and a SNES device number if more than one SNES is detected.
        Examples: "/snes", "/snes 1", "/snes localhost:23074 1" """

        snes_address = self.ctx.snes_address
        snes_device_number = -1

        options = snes_options.split()
        num_options = len(options)

        if num_options > 0:
            snes_device_number = int(options[0])

        if num_options > 1:
            snes_address = options[0]
            snes_device_number = int(options[1])

        self.ctx.snes_reconnect_address = None
        if self.ctx.snes_connect_task:
            self.ctx.snes_connect_task.cancel()
        self.ctx.snes_connect_task =  asyncio.create_task(snes_connect(self.ctx, snes_address, snes_device_number),
                                                          name="SNES Connect")
        return True

    def _cmd_snes_close(self) -> bool:
        """Close connection to a currently connected snes"""
        self.ctx.snes_reconnect_address = None
        if self.ctx.snes_socket is not None and not self.ctx.snes_socket.closed:
            asyncio.create_task(self.ctx.snes_socket.close())
            return True
        else:
            return False

    # Left here for quick re-addition for debugging.
    # def _cmd_snes_write(self, address, data):
    #     """Write the specified byte (base10) to the SNES' memory address (base16)."""
    #     if self.ctx.snes_state != SNESState.SNES_ATTACHED:
    #         self.output("No attached SNES Device.")
    #         return False
    #     snes_buffered_write(self.ctx, int(address, 16), bytes([int(data)]))
    #     asyncio.create_task(snes_flush_writes(self.ctx))
    #     self.output("Data Sent")
    #     return True

    # def _cmd_snes_read(self, address, size=1):
    #     """Read the SNES' memory address (base16)."""
    #     if self.ctx.snes_state != SNESState.SNES_ATTACHED:
    #         self.output("No attached SNES Device.")
    #         return False
    #     data = await snes_read(self.ctx, int(address, 16), size)
    #     self.output(f"Data Read: {data}")
    #     return True


class Context(CommonContext):
    command_processor = SNIClientCommandProcessor
    game = "A Link to the Past"
    items_handling = None  # set in game_watcher
    snes_connect_task: typing.Optional[asyncio.Task] = None

    def __init__(self, snes_address, server_address, password):
        super(Context, self).__init__(server_address, password)

        # snes stuff
        self.snes_address = snes_address
        self.snes_socket = None
        self.snes_state = SNESState.SNES_DISCONNECTED
        self.snes_attached_device = None
        self.snes_reconnect_address = None
        self.snes_recv_queue = asyncio.Queue()
        self.snes_request_lock = asyncio.Lock()
        self.snes_write_buffer = []
        self.snes_connector_lock = threading.Lock()
        self.death_state = DeathState.alive  # for death link flop behaviour
        self.killing_player_task = None
        self.allow_collect = False
        self.slow_mode = False

        self.awaiting_rom = False
        self.rom = None
        self.prev_rom = None

    async def connection_closed(self):
        await super(Context, self).connection_closed()
        self.awaiting_rom = False

    def event_invalid_slot(self):
        if self.snes_socket is not None and not self.snes_socket.closed:
            asyncio.create_task(self.snes_socket.close())
        raise Exception("Invalid ROM detected, "
                        "please verify that you have loaded the correct rom and reconnect your snes (/snes)")

    async def server_auth(self, password_requested: bool = False):
        if password_requested and not self.password:
            await super(Context, self).server_auth(password_requested)
        if self.rom is None:
            self.awaiting_rom = True
            snes_logger.info(
                "No ROM detected, awaiting snes connection to authenticate to the multiworld server (/snes)")
            return
        self.awaiting_rom = False
        self.auth = self.rom
        auth = base64.b64encode(self.rom).decode()
        await self.send_connect(name=auth)

    def on_deathlink(self, data: dict):
        if not self.killing_player_task or self.killing_player_task.done():
            self.killing_player_task = asyncio.create_task(deathlink_kill_player(self))
        super(Context, self).on_deathlink(data)

    async def handle_deathlink_state(self, currently_dead: bool):
        # in this state we only care about triggering a death send
        if self.death_state == DeathState.alive:
            if currently_dead:
                self.death_state = DeathState.dead
                await self.send_death()
        # in this state we care about confirming a kill, to move state to dead
        elif self.death_state == DeathState.killing_player:
            # this is being handled in deathlink_kill_player(ctx) already
            pass
        # in this state we wait until the player is alive again
        elif self.death_state == DeathState.dead:
            if not currently_dead:
                self.death_state = DeathState.alive

    async def shutdown(self):
        await super(Context, self).shutdown()
        if self.snes_connect_task:
            try:
                await asyncio.wait_for(self.snes_connect_task, 1)
            except asyncio.TimeoutError:
                self.snes_connect_task.cancel()

    def on_package(self, cmd: str, args: dict):
        if cmd in {"Connected", "RoomUpdate"}:
            if "checked_locations" in args and args["checked_locations"]:
                new_locations = set(args["checked_locations"])
                self.checked_locations |= new_locations
                self.locations_scouted |= new_locations
                # Items belonging to the player should not be marked as checked in game, since the player will likely need that item.
                # Once the games handled by SNIClient gets made to be remote items, this will no longer be needed.
                asyncio.create_task(self.send_msgs([{"cmd": "LocationScouts", "locations": list(new_locations)}]))

    def run_gui(self):
        from kvui import GameManager

        class SNIManager(GameManager):
            logging_pairs = [
                ("Client", "Archipelago"),
                ("SNES", "SNES"),
            ]
            base_title = "Archipelago SNI Client"

        self.ui = SNIManager(self)
        self.ui_task = asyncio.create_task(self.ui.async_run(), name="UI")


async def deathlink_kill_player(ctx: Context):
    ctx.death_state = DeathState.killing_player
    while ctx.death_state == DeathState.killing_player and \
            ctx.snes_state == SNESState.SNES_ATTACHED:
        if ctx.game == GAME_ALTTP:
            invincible = await snes_read(ctx, WRAM_START + 0x037B, 1)
            last_health = await snes_read(ctx, WRAM_START + 0xF36D, 1)
            await asyncio.sleep(0.25)
            health = await snes_read(ctx, WRAM_START + 0xF36D, 1)
            if not invincible or not last_health or not health:
                ctx.death_state = DeathState.dead
                ctx.last_death_link = time.time()
                continue
            if not invincible[0] and last_health[0] == health[0]:
                snes_buffered_write(ctx, WRAM_START + 0xF36D, bytes([0]))  # set current health to 0
                snes_buffered_write(ctx, WRAM_START + 0x0373,
                                    bytes([8]))  # deal 1 full heart of damage at next opportunity
        elif ctx.game == GAME_SM:
            snes_buffered_write(ctx, WRAM_START + 0x09C2, bytes([1, 0]))  # set current health to 1 (to prevent saving with 0 energy)
            snes_buffered_write(ctx, WRAM_START + 0x0A50, bytes([255])) # deal 255 of damage at next opportunity
            if not ctx.death_link_allow_survive:
                snes_buffered_write(ctx, WRAM_START + 0x09D6, bytes([0, 0]))  # set current reserve to 0
        elif ctx.game == GAME_SMW:
            from worlds.smw.Client import deathlink_kill_player as smw_deathlink_kill_player
            await smw_deathlink_kill_player(ctx)

        await snes_flush_writes(ctx)
        await asyncio.sleep(1)

        if ctx.game == GAME_ALTTP:
            gamemode = await snes_read(ctx, WRAM_START + 0x10, 1)
            if not gamemode or gamemode[0] in DEATH_MODES:
                ctx.death_state = DeathState.dead
        elif ctx.game == GAME_SM:
            gamemode = await snes_read(ctx, WRAM_START + 0x0998, 1)
            health = await snes_read(ctx, WRAM_START + 0x09C2, 2)
            if health is not None:
                health = health[0] | (health[1] << 8)
            if not gamemode or gamemode[0] in SM_DEATH_MODES or (
                    ctx.death_link_allow_survive and health is not None and health > 0):
                ctx.death_state = DeathState.dead
        elif ctx.game == GAME_DKC3:
            from worlds.dkc3.Client import deathlink_kill_player as dkc3_deathlink_kill_player
            await dkc3_deathlink_kill_player(ctx)
        ctx.last_death_link = time.time()


SNES_RECONNECT_DELAY = 5

# FXPAK Pro protocol memory mapping used by SNI
ROM_START = 0x000000

# SM
SM_ROMNAME_START = ROM_START + 0x007FC0

SM_INGAME_MODES = {0x07, 0x09, 0x0b}
SM_ENDGAME_MODES = {0x26, 0x27}
SM_DEATH_MODES = {0x15, 0x17, 0x18, 0x19, 0x1A}

# RECV and SEND are from the gameplay's perspective: SNIClient writes to RECV queue and reads from SEND queue
SM_RECV_QUEUE_START  = SRAM_START + 0x2000
SM_RECV_QUEUE_WCOUNT = SRAM_START + 0x2602
SM_SEND_QUEUE_START  = SRAM_START + 0x2700
SM_SEND_QUEUE_RCOUNT = SRAM_START + 0x2680
SM_SEND_QUEUE_WCOUNT = SRAM_START + 0x2682

SM_DEATH_LINK_ACTIVE_ADDR = ROM_START + 0x277f04    # 1 byte
SM_REMOTE_ITEM_FLAG_ADDR = ROM_START + 0x277f06    # 1 byte

# SMZ3
SMZ3_ROMNAME_START = ROM_START + 0x00FFC0

SMZ3_INGAME_MODES = {0x07, 0x09, 0x0b}
SMZ3_ENDGAME_MODES = {0x26, 0x27}
SMZ3_DEATH_MODES = {0x15, 0x17, 0x18, 0x19, 0x1A}

SMZ3_RECV_PROGRESS_ADDR = SRAM_START + 0x4000         # 2 bytes
SMZ3_RECV_ITEM_ADDR = SAVEDATA_START + 0x4D2          # 1 byte
SMZ3_RECV_ITEM_PLAYER_ADDR = SAVEDATA_START + 0x4D3   # 1 byte


class SNESState(enum.IntEnum):
    SNES_DISCONNECTED = 0
    SNES_CONNECTING = 1
    SNES_CONNECTED = 2
    SNES_ATTACHED = 3


def launch_sni():
    sni_path = Utils.get_options()["lttp_options"]["sni"]

    if not os.path.isdir(sni_path):
        sni_path = Utils.local_path(sni_path)
    if os.path.isdir(sni_path):
        dir_entry: os.DirEntry
        for dir_entry in os.scandir(sni_path):
            if dir_entry.is_file():
                lower_file = dir_entry.name.lower()
                if (lower_file.startswith("sni.") and not lower_file.endswith(".proto")) or (lower_file == "sni"):
                    sni_path = dir_entry.path
                    break

    if os.path.isfile(sni_path):
        snes_logger.info(f"Attempting to start {sni_path}")
        import sys
        if not sys.stdout:  # if it spawns a visible console, may as well populate it
            subprocess.Popen(os.path.abspath(sni_path), cwd=os.path.dirname(sni_path))
        else:
            proc = subprocess.Popen(os.path.abspath(sni_path), cwd=os.path.dirname(sni_path),
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                proc.wait(.1)  # wait a bit to see if startup fails (missing dependencies)
                snes_logger.info('Failed to start SNI. Try running it externally for error output.')
            except subprocess.TimeoutExpired:
                pass  # seems to be running

    else:
        snes_logger.info(
            f"Attempt to start SNI was aborted as path {sni_path} was not found, "
            f"please start it yourself if it is not running")


async def _snes_connect(ctx: Context, address: str):
    address = f"ws://{address}" if "://" not in address else address
    snes_logger.info("Connecting to SNI at %s ..." % address)
    seen_problems = set()
    while 1:
        try:
            snes_socket = await websockets.connect(address, ping_timeout=None, ping_interval=None)
        except Exception as e:
            problem = "%s" % e
            # only tell the user about new problems, otherwise silently lay in wait for a working connection
            if problem not in seen_problems:
                seen_problems.add(problem)
                snes_logger.error(f"Error connecting to SNI ({problem})")

                if len(seen_problems) == 1:
                    # this is the first problem. Let's try launching SNI if it isn't already running
                    launch_sni()

            await asyncio.sleep(1)
        else:
            return snes_socket


async def get_snes_devices(ctx: Context) -> typing.List[str]:
    socket = await _snes_connect(ctx, ctx.snes_address)  # establish new connection to poll
    DeviceList_Request = {
        "Opcode": "DeviceList",
        "Space": "SNES"
    }
    await socket.send(dumps(DeviceList_Request))

    reply: dict = loads(await socket.recv())
    devices: typing.List[str] = reply['Results'] if 'Results' in reply and len(reply['Results']) > 0 else []

    if not devices:
        snes_logger.info('No SNES device found. Please connect a SNES device to SNI.')
        while not devices and not ctx.exit_event.is_set():
            await asyncio.sleep(0.1)
            await socket.send(dumps(DeviceList_Request))
            reply = loads(await socket.recv())
            devices = reply['Results'] if 'Results' in reply and len(reply['Results']) > 0 else []
    if devices:
        await verify_snes_app(socket)
    await socket.close()
    return sorted(devices)


async def verify_snes_app(socket):
    AppVersion_Request = {
        "Opcode": "AppVersion",
    }
    await socket.send(dumps(AppVersion_Request))

    app: str = loads(await socket.recv())["Results"][0]
    if "SNI" not in app:
        snes_logger.warning(f"Warning: Did not find SNI as the endpoint, instead {app} was found.")


async def snes_connect(ctx: Context, address, deviceIndex=-1):
    global SNES_RECONNECT_DELAY
    if ctx.snes_socket is not None and ctx.snes_state == SNESState.SNES_CONNECTED:
        if ctx.rom:
            snes_logger.error('Already connected to SNES, with rom loaded.')
        else:
            snes_logger.error('Already connected to SNI, likely awaiting a device.')
        return

    device = None
    recv_task = None
    ctx.snes_state = SNESState.SNES_CONNECTING
    socket = await _snes_connect(ctx, address)
    ctx.snes_socket = socket
    ctx.snes_state = SNESState.SNES_CONNECTED

    try:
        devices = await get_snes_devices(ctx)
        device_count = len(devices)

        if device_count == 1:
            device = devices[0]
        elif ctx.snes_reconnect_address:
            if ctx.snes_attached_device[1] in devices:
                device = ctx.snes_attached_device[1]
            else:
                device = devices[ctx.snes_attached_device[0]]
        elif device_count > 1:
            if deviceIndex == -1:
                snes_logger.info(f"Found {device_count} SNES devices. "
                                 f"Connect to one with /snes <address> <device number>. For example /snes {address} 1")

                for idx, availableDevice in enumerate(devices):
                    snes_logger.info(str(idx + 1) + ": " + availableDevice)

            elif (deviceIndex < 0) or (deviceIndex - 1) > device_count:
                snes_logger.warning("SNES device number out of range")

            else:
                device = devices[deviceIndex - 1]

        if device is None:
            await snes_disconnect(ctx)
            return

        snes_logger.info("Attaching to " + device)

        Attach_Request = {
            "Opcode": "Attach",
            "Space": "SNES",
            "Operands": [device]
        }
        await ctx.snes_socket.send(dumps(Attach_Request))
        ctx.snes_state = SNESState.SNES_ATTACHED
        ctx.snes_attached_device = (devices.index(device), device)
        ctx.snes_reconnect_address = address
        recv_task = asyncio.create_task(snes_recv_loop(ctx))

    except Exception as e:
        if recv_task is not None:
            if not ctx.snes_socket.closed:
                await ctx.snes_socket.close()
        else:
            if ctx.snes_socket is not None:
                if not ctx.snes_socket.closed:
                    await ctx.snes_socket.close()
                ctx.snes_socket = None
            ctx.snes_state = SNESState.SNES_DISCONNECTED
        if not ctx.snes_reconnect_address:
            snes_logger.error("Error connecting to snes (%s)" % e)
        else:
            snes_logger.error(f"Error connecting to snes, attempt again in {SNES_RECONNECT_DELAY}s")
            asyncio.create_task(snes_autoreconnect(ctx))
        SNES_RECONNECT_DELAY *= 2

    else:
        SNES_RECONNECT_DELAY = ctx.starting_reconnect_delay
        snes_logger.info(f"Attached to {device}")


async def snes_disconnect(ctx: Context):
    if ctx.snes_socket:
        if not ctx.snes_socket.closed:
            await ctx.snes_socket.close()
        ctx.snes_socket = None


async def snes_autoreconnect(ctx: Context):
    await asyncio.sleep(SNES_RECONNECT_DELAY)
    if ctx.snes_reconnect_address and ctx.snes_socket is None:
        await snes_connect(ctx, ctx.snes_reconnect_address)


async def snes_recv_loop(ctx: Context):
    try:
        async for msg in ctx.snes_socket:
            ctx.snes_recv_queue.put_nowait(msg)
        snes_logger.warning("Snes disconnected")
    except Exception as e:
        if not isinstance(e, websockets.WebSocketException):
            snes_logger.exception(e)
        snes_logger.error("Lost connection to the snes, type /snes to reconnect")
    finally:
        socket, ctx.snes_socket = ctx.snes_socket, None
        if socket is not None and not socket.closed:
            await socket.close()

        ctx.snes_state = SNESState.SNES_DISCONNECTED
        ctx.snes_recv_queue = asyncio.Queue()
        ctx.hud_message_queue = []

        ctx.rom = None

        if ctx.snes_reconnect_address:
            snes_logger.info(f"...reconnecting in {SNES_RECONNECT_DELAY}s")
            asyncio.create_task(snes_autoreconnect(ctx))


async def snes_read(ctx: Context, address, size):
    try:
        await ctx.snes_request_lock.acquire()

        if ctx.snes_state != SNESState.SNES_ATTACHED or ctx.snes_socket is None or not ctx.snes_socket.open or ctx.snes_socket.closed:
            return None

        GetAddress_Request = {
            "Opcode": "GetAddress",
            "Space": "SNES",
            "Operands": [hex(address)[2:], hex(size)[2:]]
        }
        try:
            await ctx.snes_socket.send(dumps(GetAddress_Request))
        except websockets.ConnectionClosed:
            return None

        data = bytes()
        while len(data) < size:
            try:
                data += await asyncio.wait_for(ctx.snes_recv_queue.get(), 5)
            except asyncio.TimeoutError:
                break

        if len(data) != size:
            snes_logger.error('Error reading %s, requested %d bytes, received %d' % (hex(address), size, len(data)))
            if len(data):
                snes_logger.error(str(data))
                snes_logger.warning('Communication Failure with SNI')
            if ctx.snes_socket is not None and not ctx.snes_socket.closed:
                await ctx.snes_socket.close()
            return None

        return data
    finally:
        ctx.snes_request_lock.release()


async def snes_write(ctx: Context, write_list):
    try:
        await ctx.snes_request_lock.acquire()

        if ctx.snes_state != SNESState.SNES_ATTACHED or ctx.snes_socket is None or \
                not ctx.snes_socket.open or ctx.snes_socket.closed:
            return False

        PutAddress_Request = {"Opcode": "PutAddress", "Operands": [], 'Space': 'SNES'}
        try:
            for address, data in write_list:
                PutAddress_Request['Operands'] = [hex(address)[2:], hex(len(data))[2:]]
                if ctx.snes_socket is not None:
                    await ctx.snes_socket.send(dumps(PutAddress_Request))
                    await ctx.snes_socket.send(data)
                else:
                    snes_logger.warning(f"Could not send data to SNES: {data}")
        except websockets.ConnectionClosed:
            return False

        return True
    finally:
        ctx.snes_request_lock.release()


def snes_buffered_write(ctx: Context, address, data):
    if ctx.snes_write_buffer and (ctx.snes_write_buffer[-1][0] + len(ctx.snes_write_buffer[-1][1])) == address:
        # append to existing write command, bundling them
        ctx.snes_write_buffer[-1] = (ctx.snes_write_buffer[-1][0], ctx.snes_write_buffer[-1][1] + data)
    else:
        ctx.snes_write_buffer.append((address, data))


async def snes_flush_writes(ctx: Context):
    if not ctx.snes_write_buffer:
        return

    # swap buffers
    ctx.snes_write_buffer, writes = [], ctx.snes_write_buffer
    await snes_write(ctx, writes)


async def game_watcher(ctx: Context):
    prev_game_timer = 0
    perf_counter = time.perf_counter()
    while not ctx.exit_event.is_set():
        try:
            await asyncio.wait_for(ctx.watcher_event.wait(), 0.125)
        except asyncio.TimeoutError:
            pass
        ctx.watcher_event.clear()

        if not ctx.rom:
            ctx.finished_game = False
            ctx.death_link_allow_survive = False

            from worlds.dkc3.Client import dkc3_rom_init
            init_handled = await dkc3_rom_init(ctx)
            if not init_handled:
                from worlds.smw.Client import smw_rom_init
                init_handled = await smw_rom_init(ctx)
            if not init_handled:
                game_name = await snes_read(ctx, SM_ROMNAME_START, 5)
                if game_name is None:
                    continue
                elif game_name[:2] == b"SM":
                    ctx.game = GAME_SM
                    # versions lower than 0.3.0 dont have item handling flag nor remote item support
                    romVersion = int(game_name[2:5].decode('UTF-8'))
                    if romVersion < 30:
                        ctx.items_handling = 0b001 # full local
                    else:
                        item_handling = await snes_read(ctx, SM_REMOTE_ITEM_FLAG_ADDR, 1)
                        ctx.items_handling = 0b001 if item_handling is None else item_handling[0]
                else:
                    game_name = await snes_read(ctx, SMZ3_ROMNAME_START, 3)
                    if game_name == b"ZSM":
                        ctx.game = GAME_SMZ3
                        ctx.items_handling = 0b101  # local items and remote start inventory
                    else:
                        ctx.game = GAME_ALTTP
                        ctx.items_handling = 0b001  # full local

                rom = await snes_read(ctx, SM_ROMNAME_START if ctx.game == GAME_SM else SMZ3_ROMNAME_START if ctx.game == GAME_SMZ3 else ROMNAME_START,
                                      ROMNAME_SIZE)
                if rom is None or rom == bytes([0] * ROMNAME_SIZE):
                    continue

                ctx.rom = rom
                if ctx.game != GAME_SMZ3:
                    death_link = await snes_read(ctx, DEATH_LINK_ACTIVE_ADDR if ctx.game == GAME_ALTTP else
                                                 SM_DEATH_LINK_ACTIVE_ADDR, 1)
                    if death_link:
                        ctx.allow_collect = bool(death_link[0] & 0b100)
                        ctx.death_link_allow_survive = bool(death_link[0] & 0b10)
                        await ctx.update_death_link(bool(death_link[0] & 0b1))
                if not ctx.prev_rom or ctx.prev_rom != ctx.rom:
                    ctx.locations_checked = set()
                    ctx.locations_scouted = set()
                    ctx.locations_info = {}
                ctx.prev_rom = ctx.rom

            if ctx.awaiting_rom:
                await ctx.server_auth(False)
            elif ctx.server is None:
                snes_logger.warning("ROM detected but no active multiworld server connection. " +
                                    "Connect using command: /connect server:port")

        if ctx.auth and ctx.auth != ctx.rom:
            snes_logger.warning("ROM change detected, please reconnect to the multiworld server")
            await ctx.disconnect()

        if ctx.game == GAME_ALTTP:
            gamemode = await snes_read(ctx, WRAM_START + 0x10, 1)
            if "DeathLink" in ctx.tags and gamemode and ctx.last_death_link + 1 < time.time():
                currently_dead = gamemode[0] in DEATH_MODES
                await ctx.handle_deathlink_state(currently_dead)

            gameend = await snes_read(ctx, SAVEDATA_START + 0x443, 1)
            game_timer = await snes_read(ctx, SAVEDATA_START + 0x42E, 4)
            if gamemode is None or gameend is None or game_timer is None or \
                    (gamemode[0] not in INGAME_MODES and gamemode[0] not in ENDGAME_MODES):
                continue

            delay = 7 if ctx.slow_mode else 2
            if gameend[0]:
                if not ctx.finished_game:
                    await ctx.send_msgs([{"cmd": "StatusUpdate", "status": ClientStatus.CLIENT_GOAL}])
                    ctx.finished_game = True

                if time.perf_counter() - perf_counter < delay:
                    continue
                else:
                    perf_counter = time.perf_counter()
            else:
                game_timer = game_timer[0] | (game_timer[1] << 8) | (game_timer[2] << 16) | (game_timer[3] << 24)
                if abs(game_timer - prev_game_timer) < (delay * 60):
                    continue
                else:
                    prev_game_timer = game_timer

            if gamemode in ENDGAME_MODES:  # triforce room and credits
                continue

            data = await snes_read(ctx, RECV_PROGRESS_ADDR, 8)
            if data is None:
                continue

            recv_index = data[0] | (data[1] << 8)
            recv_item = data[2]
            roomid = data[4] | (data[5] << 8)
            roomdata = data[6]
            scout_location = data[7]

            if recv_index < len(ctx.items_received) and recv_item == 0:
                item = ctx.items_received[recv_index]
                recv_index += 1
                logging.info('Received %s from %s (%s) (%d/%d in list)' % (
                    color(ctx.item_names[item.item], 'red', 'bold'),
                    color(ctx.player_names[item.player], 'yellow'),
                    ctx.location_names[item.location], recv_index, len(ctx.items_received)))

                snes_buffered_write(ctx, RECV_PROGRESS_ADDR,
                                    bytes([recv_index & 0xFF, (recv_index >> 8) & 0xFF]))
                snes_buffered_write(ctx, RECV_ITEM_ADDR,
                                    bytes([item.item]))
                snes_buffered_write(ctx, RECV_ITEM_PLAYER_ADDR,
                                    bytes([min(ROM_PLAYER_LIMIT, item.player) if item.player != ctx.slot else 0]))
            if scout_location > 0 and scout_location in ctx.locations_info:
                snes_buffered_write(ctx, SCOUTREPLY_LOCATION_ADDR,
                                    bytes([scout_location]))
                snes_buffered_write(ctx, SCOUTREPLY_ITEM_ADDR,
                                    bytes([ctx.locations_info[scout_location].item]))
                snes_buffered_write(ctx, SCOUTREPLY_PLAYER_ADDR,
                                    bytes([min(ROM_PLAYER_LIMIT, ctx.locations_info[scout_location].player)]))

            await snes_flush_writes(ctx)

            if scout_location > 0 and scout_location not in ctx.locations_scouted:
                ctx.locations_scouted.add(scout_location)
                await ctx.send_msgs([{"cmd": "LocationScouts", "locations": [scout_location]}])
            await track_locations(ctx, roomid, roomdata)
        elif ctx.game == GAME_SM:
            if ctx.server is None or ctx.slot is None:
                # not successfully connected to a multiworld server, cannot process the game sending items
                continue
            gamemode = await snes_read(ctx, WRAM_START + 0x0998, 1)
            if "DeathLink" in ctx.tags and gamemode and ctx.last_death_link + 1 < time.time():
                currently_dead = gamemode[0] in SM_DEATH_MODES
                await ctx.handle_deathlink_state(currently_dead)
            if gamemode is not None and gamemode[0] in SM_ENDGAME_MODES:
                if not ctx.finished_game:
                    await ctx.send_msgs([{"cmd": "StatusUpdate", "status": ClientStatus.CLIENT_GOAL}])
                    ctx.finished_game = True
                continue

            data = await snes_read(ctx, SM_SEND_QUEUE_RCOUNT, 4)
            if data is None:
                continue

            recv_index = data[0] | (data[1] << 8)
            recv_item = data[2] | (data[3] << 8) # this is actually SM_SEND_QUEUE_WCOUNT

            while (recv_index < recv_item):
                itemAdress = recv_index * 8
                message = await snes_read(ctx, SM_SEND_QUEUE_START + itemAdress, 8)
                # worldId = message[0] | (message[1] << 8)  # unused
                # itemId = message[2] | (message[3] << 8)  # unused
                itemIndex = (message[4] | (message[5] << 8)) >> 3

                recv_index += 1
                snes_buffered_write(ctx, SM_SEND_QUEUE_RCOUNT,
                                    bytes([recv_index & 0xFF, (recv_index >> 8) & 0xFF]))

                from worlds.sm import locations_start_id
                location_id = locations_start_id + itemIndex

                ctx.locations_checked.add(location_id)
                location = ctx.location_names[location_id]
                snes_logger.info(
                    f'New Check: {location} ({len(ctx.locations_checked)}/{len(ctx.missing_locations) + len(ctx.checked_locations)})')
                await ctx.send_msgs([{"cmd": 'LocationChecks', "locations": [location_id]}])

            data = await snes_read(ctx, SM_RECV_QUEUE_WCOUNT, 2)
            if data is None:
                continue

            itemOutPtr = data[0] | (data[1] << 8)

            from worlds.sm import items_start_id
            from worlds.sm import locations_start_id
            if itemOutPtr < len(ctx.items_received):
                item = ctx.items_received[itemOutPtr]
                itemId = item.item - items_start_id
                if bool(ctx.items_handling & 0b010):
                    locationId = (item.location - locations_start_id) if (item.location >= 0 and item.player == ctx.slot) else 0xFF
                else:
                    locationId = 0x00 #backward compat

                playerID = item.player if item.player <= SM_ROM_PLAYER_LIMIT else 0
                snes_buffered_write(ctx, SM_RECV_QUEUE_START + itemOutPtr * 4, bytes(
                	[playerID & 0xFF, (playerID >> 8) & 0xFF, itemId & 0xFF, locationId & 0xFF]))
                itemOutPtr += 1
                snes_buffered_write(ctx, SM_RECV_QUEUE_WCOUNT,
                                    bytes([itemOutPtr & 0xFF, (itemOutPtr >> 8) & 0xFF]))
                logging.info('Received %s from %s (%s) (%d/%d in list)' % (
                    color(ctx.item_names[item.item], 'red', 'bold'),
                    color(ctx.player_names[item.player], 'yellow'),
                    ctx.location_names[item.location], itemOutPtr, len(ctx.items_received)))
            await snes_flush_writes(ctx)
        elif ctx.game == GAME_SMZ3:
            if ctx.server is None or ctx.slot is None:
                # not successfully connected to a multiworld server, cannot process the game sending items
                continue
            currentGame = await snes_read(ctx, SRAM_START + 0x33FE, 2)
            if (currentGame is not None):
                if (currentGame[0] != 0):
                    gamemode = await snes_read(ctx, WRAM_START + 0x0998, 1)
                    endGameModes = SM_ENDGAME_MODES
                else:
                    gamemode = await snes_read(ctx, WRAM_START + 0x10, 1)
                    endGameModes = ENDGAME_MODES

            if gamemode is not None and (gamemode[0] in endGameModes):
                if not ctx.finished_game:
                    await ctx.send_msgs([{"cmd": "StatusUpdate", "status": ClientStatus.CLIENT_GOAL}])
                    ctx.finished_game = True
                continue

            data = await snes_read(ctx, SMZ3_RECV_PROGRESS_ADDR + 0x680, 4)
            if data is None:
                continue

            recv_index = data[0] | (data[1] << 8)
            recv_item = data[2] | (data[3] << 8)

            while (recv_index < recv_item):
                itemAdress = recv_index * 8
                message = await snes_read(ctx, SMZ3_RECV_PROGRESS_ADDR + 0x700 + itemAdress, 8)
                # worldId = message[0] | (message[1] << 8)  # unused
                # itemId = message[2] | (message[3] << 8)  # unused
                isZ3Item = ((message[5] & 0x80) != 0)
                maskedPart = (message[5] & 0x7F) if isZ3Item else message[5]
                itemIndex = ((message[4] | (maskedPart << 8)) >> 3) + (256 if isZ3Item else 0)

                recv_index += 1
                snes_buffered_write(ctx, SMZ3_RECV_PROGRESS_ADDR + 0x680, bytes([recv_index & 0xFF, (recv_index >> 8) & 0xFF]))

                from worlds.smz3.TotalSMZ3.Location import locations_start_id
                from worlds.smz3 import convertLocSMZ3IDToAPID
                location_id = locations_start_id + convertLocSMZ3IDToAPID(itemIndex)

                ctx.locations_checked.add(location_id)
                location = ctx.location_names[location_id]
                snes_logger.info(f'New Check: {location} ({len(ctx.locations_checked)}/{len(ctx.missing_locations) + len(ctx.checked_locations)})')
                await ctx.send_msgs([{"cmd": 'LocationChecks', "locations": [location_id]}])

            data = await snes_read(ctx, SMZ3_RECV_PROGRESS_ADDR + 0x600, 4)
            if data is None:
                continue

            # recv_itemOutPtr = data[0] | (data[1] << 8) # unused
            itemOutPtr = data[2] | (data[3] << 8)

            from worlds.smz3.TotalSMZ3.Item import items_start_id
            if itemOutPtr < len(ctx.items_received):
                item = ctx.items_received[itemOutPtr]
                itemId = item.item - items_start_id

                playerID = item.player if item.player <= SMZ3_ROM_PLAYER_LIMIT else 0
                snes_buffered_write(ctx, SMZ3_RECV_PROGRESS_ADDR + itemOutPtr * 4, bytes([playerID & 0xFF, (playerID >> 8) & 0xFF, itemId & 0xFF, (itemId >> 8) & 0xFF]))
                itemOutPtr += 1
                snes_buffered_write(ctx, SMZ3_RECV_PROGRESS_ADDR + 0x602, bytes([itemOutPtr & 0xFF, (itemOutPtr >> 8) & 0xFF]))
                logging.info('Received %s from %s (%s) (%d/%d in list)' % (
                    color(ctx.item_names[item.item], 'red', 'bold'), color(ctx.player_names[item.player], 'yellow'),
                    ctx.location_names[item.location], itemOutPtr, len(ctx.items_received)))
            await snes_flush_writes(ctx)
        elif ctx.game == GAME_DKC3:
            from worlds.dkc3.Client import dkc3_game_watcher
            await dkc3_game_watcher(ctx)
        elif ctx.game == GAME_SMW:
            from worlds.smw.Client import smw_game_watcher
            await smw_game_watcher(ctx)


async def run_game(romfile):
    auto_start = Utils.get_options()["lttp_options"].get("rom_start", True)
    if auto_start is True:
        import webbrowser
        webbrowser.open(romfile)
    elif os.path.isfile(auto_start):
        subprocess.Popen([auto_start, romfile],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def main():
    multiprocessing.freeze_support()
    parser = get_base_parser()
    parser.add_argument('diff_file', default="", type=str, nargs="?",
                        help='Path to a Archipelago Binary Patch file')
    parser.add_argument('--snes', default='localhost:23074', help='Address of the SNI server.')
    parser.add_argument('--loglevel', default='info', choices=['debug', 'info', 'warning', 'error', 'critical'])
    args = parser.parse_args()

    if args.diff_file:
        import Patch
        logging.info("Patch file was supplied. Creating sfc rom..")
        try:
            meta, romfile = Patch.create_rom_file(args.diff_file)
        except Exception as e:
            Utils.messagebox('Error', str(e), True)
            raise
        args.connect = meta["server"]
        logging.info(f"Wrote rom file to {romfile}")
        if args.diff_file.endswith(".apsoe"):
            import webbrowser
            webbrowser.open(f"http://www.evermizer.com/apclient/#server={meta['server']}")
            logging.info("Starting Evermizer Client in your Browser...")
            import time
            time.sleep(3)
            sys.exit()
        elif args.diff_file.endswith(".aplttp"):
            adjustedromfile, adjusted = get_alttp_settings(romfile)
            asyncio.create_task(run_game(adjustedromfile if adjusted else romfile))
        else:
            asyncio.create_task(run_game(romfile))

    ctx = Context(args.snes, args.connect, args.password)
    if ctx.server_task is None:
        ctx.server_task = asyncio.create_task(server_loop(ctx), name="ServerLoop")

    if gui_enabled:
        ctx.run_gui()
    ctx.run_cli()

    ctx.snes_connect_task = asyncio.create_task(snes_connect(ctx, ctx.snes_address), name="SNES Connect")
    watcher_task = asyncio.create_task(game_watcher(ctx), name="GameWatcher")

    await ctx.exit_event.wait()

    ctx.server_address = None
    ctx.snes_reconnect_address = None
    if ctx.snes_socket is not None and not ctx.snes_socket.closed:
        await ctx.snes_socket.close()
    await watcher_task
    await ctx.shutdown()


if __name__ == '__main__':
    colorama.init()
    asyncio.run(main())
    colorama.deinit()

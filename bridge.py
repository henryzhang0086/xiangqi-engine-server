#!/usr/bin/env python3
"""
Elite Cloud 象棋引擎 - 云端多实例服务
支持: Pikafish / 任意 UCI 象棋引擎
并发: 每个 WebSocket 连接独享一个引擎进程（支持10+并发）

用法:
  python3 xiangqi_bridge.py                        # 默认 ./pikafish 端口8765
  python3 xiangqi_bridge.py --engine ./pikafish    # 指定引擎
  python3 xiangqi_bridge.py --port 8765            # 指定端口
  python3 xiangqi_bridge.py --max-clients 10       # 最大并发数
  python3 xiangqi_bridge.py --threads 2            # 每个引擎线程数
  python3 xiangqi_bridge.py --hash 256             # 每个引擎哈希表MB
"""

import asyncio
import subprocess
import sys
import json
import signal
import os
import time
import argparse
import logging
from pathlib import Path
from datetime import datetime

# ── 自动安装依赖 ──────────────────────────────────────────────────
try:
    import websockets
except ImportError:
    print("安装 websockets...")
    os.system(f"{sys.executable} -m pip install websockets -q")
    import websockets

# ── 日志配置 ──────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger('bridge')

# ── 参数解析 ──────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description='象棋引擎 WebSocket 桥接服务')
parser.add_argument('--engine',      default='./pikafish', help='引擎路径')
parser.add_argument('--port',        type=int, default=8765, help='WebSocket 端口')
parser.add_argument('--host',        default='0.0.0.0', help='监听地址（0.0.0.0=公网）')
parser.add_argument('--max-clients', type=int, default=10, help='最大并发连接数')
parser.add_argument('--threads',     type=int, default=2,  help='每个引擎线程数')
parser.add_argument('--hash',        type=int, default=128, help='每个引擎哈希表MB')
parser.add_argument('--timeout',     type=int, default=300, help='无活动超时秒数')
args = parser.parse_args()

# ── 全局统计 ──────────────────────────────────────────────────────
stats = {
    'total_connections': 0,
    'active_connections': 0,
    'rejected_connections': 0,
    'start_time': time.time(),
}

# ══════════════════════════════════════════════════════════════════
# 引擎实例（每个连接独立）
# ══════════════════════════════════════════════════════════════════
class EngineInstance:
    def __init__(self, client_id: str):
        self.client_id = client_id
        self.proc = None
        self.ready = False
        self.busy = False
        self.loop = None

    def start(self) -> bool:
        path = Path(args.engine)
        if not path.exists():
            log.error(f"[{self.client_id}] 引擎文件不存在: {args.engine}")
            return False
        try:
            path.chmod(path.stat().st_mode | 0o111)
            self.proc = subprocess.Popen(
                [str(path.resolve())],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
                text=True,
            )
            self.loop = asyncio.get_event_loop()
            log.info(f"[{self.client_id}] 引擎启动 PID={self.proc.pid}")
            return True
        except Exception as e:
            log.error(f"[{self.client_id}] 引擎启动失败: {e}")
            return False

    def send(self, cmd: str):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.write(cmd + "\n")
                self.proc.stdin.flush()
            except Exception as e:
                log.warning(f"[{self.client_id}] 发送命令失败: {e}")

    async def read_line(self) -> str:
        """异步读取引擎输出一行"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.proc.stdout.readline)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.send("quit")
                self.proc.wait(timeout=2)
            except Exception:
                self.proc.kill()
            log.info(f"[{self.client_id}] 引擎已停止")

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

# ══════════════════════════════════════════════════════════════════
# 联网对战房间中转
# ══════════════════════════════════════════════════════════════════
online_rooms = {}  # room_id -> {'red': ws, 'black': ws}

async def handle_online_room(websocket, client_id, msg):
    room = str(msg.get('room', ''))
    action = msg.get('action', '')
    role = msg.get('role', 'red')
    log.info(f"[{client_id}] 联网对战: {action} room={room} role={role}")

    if action == 'create':
        online_rooms[room] = {'red': websocket, 'black': None}
        await websocket.send(json.dumps({"type": "room_created", "room": room}))
        # 等待对手加入
        try:
            async for raw in websocket:
                try:
                    d = json.loads(raw)
                    if d.get('type') == 'online_move':
                        opponent = online_rooms.get(room, {}).get('black')
                        if opponent:
                            await opponent.send(json.dumps({"type": "online_move", "move": d['move']}))
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            if room in online_rooms:
                opponent = online_rooms[room].get('black')
                if opponent:
                    try: await opponent.send(json.dumps({"type": "opponent_left"}))
                    except: pass
                del online_rooms[room]

    elif action == 'join':
        if room not in online_rooms or online_rooms[room]['red'] is None:
            await websocket.send(json.dumps({"type": "error", "message": "房间不存在或已满"}))
            return
        online_rooms[room]['black'] = websocket
        # 通知双方
        red_ws = online_rooms[room]['red']
        try:
            await red_ws.send(json.dumps({"type": "opponent_joined"}))
            await websocket.send(json.dumps({"type": "room_joined", "room": room}))
        except Exception:
            return
        # 转发走棋
        try:
            async for raw in websocket:
                try:
                    d = json.loads(raw)
                    if d.get('type') == 'online_move':
                        opponent = online_rooms.get(room, {}).get('red')
                        if opponent:
                            await opponent.send(json.dumps({"type": "online_move", "move": d['move']}))
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            if room in online_rooms:
                opponent = online_rooms[room].get('red')
                if opponent:
                    try: await opponent.send(json.dumps({"type": "opponent_left"}))
                    except: pass
                online_rooms[room]['black'] = None


# ══════════════════════════════════════════════════════════════════
# WebSocket 连接处理
# ══════════════════════════════════════════════════════════════════
async def handle_client(websocket):
    # ── 并发限制检查 ──
    if stats['active_connections'] >= args.max_clients:
        stats['rejected_connections'] += 1
        await websocket.send(json.dumps({
            "type": "error",
            "code": "SERVER_FULL",
            "message": f"服务器已满（最大{args.max_clients}个连接），请稍后重试"
        }))
        log.warning(f"连接被拒绝（已满 {stats['active_connections']}/{args.max_clients}）")
        return

    # ── 创建客户端 ID ──
    stats['total_connections'] += 1
    stats['active_connections'] += 1
    client_id = f"C{stats['total_connections']:04d}"
    addr = getattr(websocket, 'remote_address', ('?', 0))
    log.info(f"[{client_id}] 连接来自 {addr[0]}:{addr[1]}  活跃:{stats['active_connections']}/{args.max_clients}")

    # ── 检查是否为联网对战房间请求 ──
    try:
        first_raw = await asyncio.wait_for(websocket.recv(), timeout=5)
        first_msg = json.loads(first_raw)
    except Exception:
        stats['active_connections'] -= 1
        return

    if first_msg.get('type') == 'room':
        await handle_online_room(websocket, client_id, first_msg)
        stats['active_connections'] -= 1
        return

    # ── 否则走引擎模式，将first_msg当作第一条命令处理 ──
    # ── 启动专属引擎 ──
    engine = EngineInstance(client_id)
    if not engine.start():
        await websocket.send(json.dumps({
            "type": "error",
            "code": "ENGINE_FAILED",
            "message": "引擎启动失败，请联系管理员"
        }))
        stats['active_connections'] -= 1
        return

    # ── 发送连接成功消息 ──
    await websocket.send(json.dumps({
        "type": "connected",
        "client_id": client_id,
        "engine": "Elite Cloud",
        "pid": engine.proc.pid,
        "server_stats": {
            "active": stats['active_connections'],
            "max": args.max_clients,
            "total_served": stats['total_connections'],
        }
    }))

    # ── 初始化引擎 ──
    engine.send("uci")
    engine.send(f"setoption name Threads value {args.threads}")
    engine.send(f"setoption name Hash value {args.hash}")
    engine.send("isready")

    # ── 引擎输出转发任务 ──
    reader_task = asyncio.create_task(_engine_reader(client_id, engine, websocket))
    last_activity = time.time()

    # 处理第一条消息（非room类型的命令）
    async def process_msg(msg):
        nonlocal last_activity
        last_activity = time.time()
        msg_type = msg.get("type", "")
        if msg_type == "cmd":
            cmd = msg.get("cmd", "").strip()
            if cmd and _is_safe_cmd(cmd):
                engine.send(cmd)
        elif msg_type == "ping":
            await websocket.send(json.dumps({"type": "pong"}))

    if first_msg.get('type') in ('cmd', 'ping'):
        await process_msg(first_msg)

    try:
        async for raw_msg in websocket:
            last_activity = time.time()
            try:
                msg = json.loads(raw_msg)
                msg_type = msg.get("type", "")

                if msg_type == "cmd":
                    cmd = msg.get("cmd", "").strip()
                    if cmd:
                        # 安全过滤：不允许客户端执行危险命令
                        if _is_safe_cmd(cmd):
                            engine.send(cmd)
                        else:
                            log.warning(f"[{client_id}] 拦截危险命令: {cmd}")

                elif msg_type == "ping":
                    await websocket.send(json.dumps({"type": "pong"}))

                elif msg_type == "stats":
                    await websocket.send(json.dumps({
                        "type": "stats",
                        "active": stats['active_connections'],
                        "max": args.max_clients,
                        "uptime": int(time.time() - stats['start_time']),
                        "total_served": stats['total_connections'],
                    }))

            except json.JSONDecodeError:
                # 纯文本命令兼容
                cmd = raw_msg.strip()
                if cmd and _is_safe_cmd(cmd):
                    engine.send(cmd)

            # 超时检测
            if time.time() - last_activity > args.timeout:
                log.info(f"[{client_id}] 超时断开（{args.timeout}秒无活动）")
                break

    except websockets.exceptions.ConnectionClosed:
        log.info(f"[{client_id}] 连接正常关闭")
    except Exception as e:
        log.error(f"[{client_id}] 连接异常: {e}")
    finally:
        reader_task.cancel()
        engine.stop()
        stats['active_connections'] -= 1
        log.info(f"[{client_id}] 清理完毕  活跃:{stats['active_connections']}/{args.max_clients}")


async def _engine_reader(client_id: str, engine: EngineInstance, websocket):
    """持续读取引擎输出，转发给前端"""
    while True:
        try:
            line = await engine.read_line()
            if not line:
                log.warning(f"[{client_id}] 引擎进程已退出")
                await websocket.send(json.dumps({
                    "type": "error",
                    "code": "ENGINE_DIED",
                    "message": "引擎进程意外退出"
                }))
                break
            line = line.strip()
            if line:
                await websocket.send(json.dumps({"type": "output", "line": line}))
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"[{client_id}] 读取引擎输出失败: {e}")
            break


def _is_safe_cmd(cmd: str) -> bool:
    """白名单过滤，只允许标准 UCI 命令"""
    safe_prefixes = (
        'uci', 'isready', 'ucinewgame', 'stop', 'quit',
        'position', 'go ', 'setoption name Hash',
        'setoption name Threads', 'setoption name MultiPV',
        'setoption name UCI_', 'ponderhit', 'd', 'eval',
    )
    return any(cmd.startswith(p) for p in safe_prefixes)


# ══════════════════════════════════════════════════════════════════
# 启动服务
# ══════════════════════════════════════════════════════════════════
async def main():
    # 检查引擎
    engine_path = Path(args.engine)
    if not engine_path.exists():
        log.error(f"引擎文件不存在: {args.engine}")
        log.error("请先下载 Pikafish: https://github.com/official-pikafish/Pikafish/releases")
        sys.exit(1)

    engine_path.chmod(engine_path.stat().st_mode | 0o111)

    # 测试引擎启动
    log.info("测试引擎启动...")
    try:
        test = subprocess.run(
            [str(engine_path.resolve())],
            input="uci\nquit\n", capture_output=True, text=True, timeout=5
        )
        if "uciok" in test.stdout:
            # 提取引擎名
            for line in test.stdout.splitlines():
                if line.startswith("id name"):
                    log.info(f"引擎就绪: {line.replace('id name ','')}")
                    break
        else:
            log.warning("引擎响应异常，请确认文件正确")
    except Exception as e:
        log.error(f"引擎测试失败: {e}")
        sys.exit(1)

    print()
    print("=" * 60)
    print("  🎮 Elite Cloud 象棋引擎服务")
    print("=" * 60)
    print(f"  引擎:     {args.engine}")
    print(f"  地址:     ws://<服务器IP>:{args.port}")
    print(f"  最大并发: {args.max_clients} 个连接")
    print(f"  线程/实例: {args.threads}")
    print(f"  哈希/实例: {args.hash} MB")
    print(f"  内存估算: {args.max_clients * args.hash} MB (满载)")
    print(f"  超时:     {args.timeout} 秒")
    print("=" * 60)
    print()

    stop = asyncio.Future()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set_result, None)
        except Exception:
            pass

    try:
        async with websockets.serve(
            handle_client,
            args.host,
            args.port,
            ping_interval=30,
            ping_timeout=10,
            max_size=1024 * 1024,  # 1MB 消息上限
        ):
            log.info(f"WebSocket 服务就绪: ws://0.0.0.0:{args.port}")
            log.info("按 Ctrl+C 停止服务")
            await stop
    except OSError as e:
        log.error(f"端口 {args.port} 无法绑定: {e}")
        sys.exit(1)

    log.info("服务关闭中...")


if __name__ == "__main__":
    asyncio.run(main())

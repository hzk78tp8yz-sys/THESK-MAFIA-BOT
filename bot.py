import asyncio
import html
import logging
import os
import random
import sqlite3
import time
from collections import Counter

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

TOKEN = os.getenv("BOT_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "mafia.db")
MIN_PLAYERS = 4
MAX_PLAYERS = 30
MAX_ROUNDS = 30
GROUP = {"group", "supergroup"}

ROLES = {
    "mafia": ("🕴 Mafiya", "Har kecha bitta odamni o'ldirasiz. Jamoangiz bilan kelishing."),
    "don": ("🎩 Don", "Mafiya boshlig'i. Ovozingiz kuchliroq. Komissar sizni tinch aholi deb ko'radi."),
    "lawyer": ("⚖️ Advokat", "Har kecha bitta mafiyachini komissar tekshiruvidan himoya qilasiz. Siz ham o'ldirishda ovoz berasiz."),
    "doctor": ("👨‍⚕️ Doktor", "Har kecha bitta odamni qutqarasiz (o'zingizni ham). Ketma-ket bir odamni ikki marta emas."),
    "commissar": ("🕵️ Komissar", "Har kecha bitta odamni tekshirasiz: mafiyami yoki yo'q."),
    "maniac": ("🔪 Manyak", "Yolg'iz o'ynaysiz. Har kecha bitta odamni o'ldirasiz. Oxirgi qolsangiz yutasiz."),
    "civil": ("👨‍🌾 Tinch aholi", "Kunduzi muhokama qiling va mafiyani ovoz berib toping."),
}
MAFIA_TEAM = {"mafia", "don", "lawyer"}


def team_of(role: str) -> str:
    return "mafia" if role in MAFIA_TEAM else ("maniac" if role == "maniac" else "civil")


# ---------------- Baza ----------------

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY, name TEXT,
    coins INTEGER DEFAULT 100, diamonds INTEGER DEFAULT 1,
    games INTEGER DEFAULT 0, wins INTEGER DEFAULT 0,
    last_bonus INTEGER DEFAULT 0, pending_role TEXT,
    shield INTEGER DEFAULT 0, fake INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS chats(
    id INTEGER PRIMARY KEY,
    reg INTEGER DEFAULT 60, night INTEGER DEFAULT 40, day INTEGER DEFAULT 45,
    maniac INTEGER DEFAULT 1, lawyer INTEGER DEFAULT 1, don INTEGER DEFAULT 1);
""")
db.commit()


def user(uid: int, name: str | None = None):
    row = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not row:
        db.execute("INSERT INTO users(id,name) VALUES(?,?)", (uid, name or str(uid)))
        db.commit()
        return db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if name and row["name"] != name:
        db.execute("UPDATE users SET name=? WHERE id=?", (name, uid))
        db.commit()
    return row


def add(uid: int, **kw):
    sets = ", ".join(f"{k}={k}+?" for k in kw)
    db.execute(f"UPDATE users SET {sets} WHERE id=?", (*kw.values(), uid))
    db.commit()


def upd(uid: int, **kw):
    sets = ", ".join(f"{k}=?" for k in kw)
    db.execute(f"UPDATE users SET {sets} WHERE id=?", (*kw.values(), uid))
    db.commit()


def cfg(chat_id: int):
    db.execute("INSERT OR IGNORE INTO chats(id) VALUES(?)", (chat_id,))
    db.commit()
    return db.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()


# ---------------- O'yin ----------------

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML")) if TOKEN else None
dp = Dispatcher()
games: dict[int, "Game"] = {}


class Game:
    def __init__(self, chat_id: int, owner: int):
        self.chat_id = chat_id
        self.owner = owner
        self.cfg = dict(cfg(chat_id))
        self.players: dict[int, dict] = {}
        self.state = "reg"
        self.start_event = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.round = 0
        self.last_heal = None
        self.votes: dict[int, int] = {}
        self.new_night()

    def new_night(self):
        self.mafia_votes: dict[int, int] = {}
        self.heal = None
        self.protect = None
        self.maniac_target = None
        self.check = None

    def alive(self, role: str | None = None):
        return [u for u, p in self.players.items()
                if p["alive"] and (role is None or p["role"] == role)]

    def alive_mafia(self):
        return [u for u in self.alive() if self.players[u]["role"] in MAFIA_TEAM]

    def name(self, uid: int) -> str:
        return f'<a href="tg://user?id={uid}">{html.escape(self.players[uid]["name"])}</a>'


async def safe_send(uid, text, **kw):
    try:
        return await bot.send_message(uid, text, **kw)
    except Exception as e:
        logging.warning("Yuborib bo'lmadi %s: %s", uid, e)


async def safe_edit(msg, text, kb=None):
    try:
        await msg.edit_text(text, reply_markup=kb)
    except Exception:
        pass


def kb_for(g: Game, prefix: str, ids, skip: bool = False):
    kb = InlineKeyboardBuilder()
    for u in ids:
        kb.row(InlineKeyboardButton(
            text=g.players[u]["name"][:30], callback_data=f"{prefix}:{g.chat_id}:{u}"))
    if skip:
        kb.row(InlineKeyboardButton(
            text="⏭ O'tkazib yuborish", callback_data=f"{prefix}:{g.chat_id}:0"))
    return kb.as_markup()


def winner(g: Game):
    al = g.alive()
    if not al:
        return "draw"
    m = len(g.alive_mafia())
    k = len(g.alive("maniac"))
    c = len(al) - m - k
    if m == 0 and k == 0:
        return "civil"
    if m == 0 and k > 0 and c <= 1:
        return "maniac"
    if k == 0 and m >= c:
        return "mafia"
    return None


async def assign_roles(g: Game):
    c = g.cfg
    ids = list(g.players)
    random.shuffle(ids)
    n = len(ids)
    mn = max(1, n // 4)
    slots = ["mafia"] * mn
    if c["don"] and mn >= 2:
        slots[0] = "don"
    if c["lawyer"] and mn >= 3:
        slots[1] = "lawyer"
    roles = slots + ["doctor"]
    if n >= 5:
        roles.append("commissar")
    if c["maniac"] and n >= 7:
        roles.append("maniac")
    roles += ["civil"] * (n - len(roles))
    for u, r in zip(ids, roles):
        g.players[u]["role"] = r

    # sotib olingan rol kartalari
    for u in ids:
        pr = user(u)["pending_role"]
        if pr and pr in roles:
            if g.players[u]["role"] != pr:
                holder = next((h for h in ids if g.players[h]["role"] == pr
                               and not user(h)["pending_role"]), None)
                if not holder:
                    continue
                g.players[holder]["role"] = g.players[u]["role"]
                g.players[u]["role"] = pr
            upd(u, pending_role=None)

    # himoya va soxta hujjat
    for u in ids:
        row = user(u)
        if row["shield"] > 0:
            add(u, shield=-1)
            g.players[u]["shield"] = True
        if row["fake"] > 0:
            add(u, fake=-1)
            g.players[u]["fake"] = True

    mafia_ids = [u for u in ids if g.players[u]["role"] in MAFIA_TEAM]
    for u in ids:
        r = g.players[u]["role"]
        text = f"Sizning rolingiz: <b>{ROLES[r][0]}</b>\n{ROLES[r][1]}"
        if g.players[u].get("shield"):
            text += "\n🛡 Himoyangiz faol."
        if g.players[u].get("fake"):
            text += "\n📄 Soxta hujjatingiz faol."
        if r in MAFIA_TEAM and len(mafia_ids) > 1:
            text += "\n\nMafiya jamoasi:\n" + "\n".join(
                f"{ROLES[g.players[x]['role']][0]} — {g.name(x)}" for x in mafia_ids)
        await safe_send(u, text)
        await asyncio.sleep(0.05)
    await bot.send_message(
        g.chat_id,
        f"🎬 <b>O'yin boshlandi!</b> Jami: {n}\n"
        f"Mafiya jamoasi: {len(mafia_ids)}"
        f"{', manyak bor' if 'maniac' in roles else ''}.\n"
        "Rollar shaxsiy xabarda yuborildi.")


async def night(g: Game):
    g.new_night()
    g.state = "night"
    await bot.send_message(
        g.chat_id,
        f"🌙 <b>{g.round}-kecha.</b> Shahar uxlaydi. Maxsus rollar, shaxsiy xabarlarni tekshiring.")
    al = g.alive()
    mteam = set(g.alive_mafia())
    sends = []
    for u in al:
        r = g.players[u]["role"]
        if r in MAFIA_TEAM:
            sends.append(safe_send(u, "🕴 Kimni o'ldiramiz?",
                                   reply_markup=kb_for(g, "n", [x for x in al if x not in mteam])))
        if r == "lawyer":
            sends.append(safe_send(u, "⚖️ Kimni himoya qilasiz?",
                                   reply_markup=kb_for(g, "p", list(mteam))))
        elif r == "doctor":
            sends.append(safe_send(u, "👨‍⚕️ Kimni qutqarasiz?",
                                   reply_markup=kb_for(g, "n", [x for x in al if x != g.last_heal])))
        elif r == "maniac":
            sends.append(safe_send(u, "🔪 Kimni o'ldirasiz?",
                                   reply_markup=kb_for(g, "n", [x for x in al if x != u])))
        elif r == "commissar":
            sends.append(safe_send(u, "🕵️ Kimni tekshirasiz?",
                                   reply_markup=kb_for(g, "n", [x for x in al if x != u])))
    await asyncio.gather(*sends)

    for _ in range(g.cfg["night"]):
        await asyncio.sleep(1)
        done = (
            len(g.mafia_votes) >= len(mteam)
            and (g.heal is not None or not g.alive("doctor"))
            and (g.maniac_target is not None or not g.alive("maniac"))
            and (g.check is not None or not g.alive("commissar"))
            and (g.protect is not None or not g.alive("lawyer"))
        )
        if done:
            break
    g.state = "resolve"

    # mafiya nishoni
    mt = None
    if g.mafia_votes:
        w: Counter = Counter()
        for voter, tgt in g.mafia_votes.items():
            w[tgt] += 1.5 if g.players[voter]["role"] == "don" else 1
        best = max(w.values())
        mt = random.choice([t for t, v in w.items() if v == best])

    lines = []
    dead = []
    for t in {x for x in (mt, g.maniac_target) if x}:
        if t == g.heal:
            lines.append("💉 Doktor kimnidir o'limdan qutqarib qoldi!")
        elif g.players[t].get("shield"):
            g.players[t]["shield"] = False
            lines.append("🛡 Kimdir himoya vositasi tufayli omon qoldi!")
        else:
            g.players[t]["alive"] = False
            dead.append(t)
    g.last_heal = g.heal

    text = f"☀️ <b>{g.round}-tong otdi.</b>\n"
    if dead:
        for t in dead:
            text += f"💀 Bu kecha {g.name(t)} o'ldirildi. U — {ROLES[g.players[t]['role']][0]} edi.\n"
    elif not lines:
        text += "Bu kecha hech kim o'lmadi.\n"
    text += "\n".join(lines)
    await bot.send_message(g.chat_id, text)

    # komissar natijasi
    if g.check:
        cu, tgt = g.check
        p = g.players[tgt]
        is_mafia = (p["role"] in ("mafia", "lawyer") and tgt != g.protect
                    and not p.get("fake"))
        res = "🕴 MAFIYA" if is_mafia else "✅ Mafiya emas"
        await safe_send(cu, f"🕵️ Tekshiruv natijasi: {html.escape(p['name'])} — {res}")


async def day(g: Game):
    g.votes = {}
    g.state = "day"
    al = g.alive()
    await bot.send_message(
        g.chat_id,
        f"🗣 Muhokama va ovoz berish ({g.cfg['day']} soniya). Kimni osamiz?\n"
        f"Tirik: {len(al)}",
        reply_markup=kb_for(g, "v", al, skip=True))
    for _ in range(g.cfg["day"]):
        await asyncio.sleep(1)
        if len(g.votes) >= len(g.alive()):
            break
    g.state = "resolve"
    real = Counter(t for t in g.votes.values() if t)
    skips = sum(1 for t in g.votes.values() if t == 0)
    if not real:
        return await bot.send_message(g.chat_id, "Hech kim ovoz bermadi. Hech kim osilmadi.")
    tally = "\n".join(f"• {g.name(t)} — {n}" for t, n in real.most_common())
    top = real.most_common()
    if (len(top) > 1 and top[0][1] == top[1][1]) or skips >= top[0][1]:
        return await bot.send_message(
            g.chat_id, f"📊 Ovozlar:\n{tally}\n\nOvozlar teng yoki o'tkazildi. Hech kim osilmadi.")
    victim = top[0][0]
    g.players[victim]["alive"] = False
    await bot.send_message(
        g.chat_id,
        f"📊 Ovozlar:\n{tally}\n\n⚖️ {g.name(victim)} osildi. "
        f"U — {ROLES[g.players[victim]['role']][0]} edi.")


async def finish(g: Game, win: str):
    titles = {
        "civil": "👨‍🌾 Tinch aholi g'alaba qildi!",
        "mafia": "🕴 Mafiya g'alaba qildi!",
        "maniac": "🔪 Manyak g'alaba qildi!",
        "draw": "🤝 Durang!",
    }
    lines = []
    for u, p in g.players.items():
        won = win != "draw" and team_of(p["role"]) == win
        coins = (40 if won else 10) + (10 if p["alive"] else 0)
        add(u, games=1, wins=1 if won else 0, coins=coins)
        lines.append(f"{'✅' if p['alive'] else '💀'} {g.name(u)} — "
                     f"{ROLES[p['role']][0]}{' 🏆' if won else ''} (+{coins}💰)")
    await bot.send_message(g.chat_id, f"🏁 <b>{titles[win]}</b>\n\n" + "\n".join(lines))


async def run_game(g: Game):
    try:
        try:
            await asyncio.wait_for(g.start_event.wait(), g.cfg["reg"])
        except asyncio.TimeoutError:
            pass
        if len(g.players) < MIN_PLAYERS:
            await bot.send_message(
                g.chat_id, f"😕 O'yinchilar yetarli emas (kamida {MIN_PLAYERS}). Bekor qilindi.")
            return
        g.state = "play"
        await assign_roles(g)
        while True:
            g.round += 1
            await night(g)
            w = winner(g)
            if not w:
                await day(g)
                w = winner(g)
            if not w and g.round >= MAX_ROUNDS:
                w = "draw"
            if w:
                await finish(g, w)
                break
    except asyncio.CancelledError:
        pass
    except Exception:
        logging.exception("O'yin xatosi")
        try:
            await bot.send_message(
                g.chat_id, "⚠️ Xatolik yuz berdi, o'yin to'xtatildi. /game bilan qayta boshlang.")
        except Exception:
            pass
    finally:
        if games.get(g.chat_id) is g:
            games.pop(g.chat_id, None)


# ---------------- Buyruqlar ----------------

HELP = (
    "🎭 <b>Mafia bot</b>\n\n"
    "Guruhda:\n/game — o'yin boshlash\n/startgame — erta boshlash\n"
    "/leave — ro'yxatdan chiqish\n/stop — o'yinni to'xtatish\n/settings — sozlamalar (admin)\n\n"
    "Hamma joyda:\n/profile — profil\n/top — reyting\n/shop — do'kon\n/bonus — kunlik bonus"
)


@dp.message(Command("game"), F.chat.type.in_(GROUP))
async def cmd_game(m: Message):
    if not m.from_user:
        return
    if m.chat.id in games:
        return await m.reply("Bu guruhda o'yin allaqachon boshlangan.")
    user(m.from_user.id, m.from_user.full_name)
    g = Game(m.chat.id, m.from_user.id)
    games[m.chat.id] = g
    me = await bot.get_me()
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(
        text="➕ Qo'shilish", url=f"https://t.me/{me.username}?start=j{m.chat.id}"))
    await m.answer(
        f"🎭 <b>Mafia o'yini!</b>\nRo'yxat {g.cfg['reg']} soniya ochiq. "
        f"Kamida {MIN_PLAYERS} o'yinchi kerak.\nErta boshlash: /startgame",
        reply_markup=kb.as_markup())
    g.task = asyncio.create_task(run_game(g))


@dp.message(CommandStart(), F.chat.type == "private")
async def cmd_start(m: Message, command: CommandObject):
    u = m.from_user
    user(u.id, u.full_name)
    arg = command.args or ""
    if not arg.startswith("j"):
        return await m.answer(HELP)
    try:
        chat_id = int(arg[1:])
    except ValueError:
        return
    g = games.get(chat_id)
    if not g or g.state != "reg":
        return await m.answer("Ro'yxat yopilgan yoki o'yin yo'q.")
    if u.id in g.players:
        return await m.answer("Siz allaqachon ro'yxatdasiz ✅")
    if len(g.players) >= MAX_PLAYERS:
        return await m.answer("O'yin to'lgan.")
    g.players[u.id] = {"name": u.full_name, "role": None, "alive": True}
    await m.answer("Ro'yxatga qo'shildingiz ✅ Guruhga qayting.")
    await safe_send(chat_id, f"✅ {g.name(u.id)} qo'shildi. Jami: {len(g.players)}")


@dp.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer(HELP)


@dp.message(Command("startgame"), F.chat.type.in_(GROUP))
async def cmd_startgame(m: Message):
    g = games.get(m.chat.id)
    if g and g.state == "reg":
        g.start_event.set()


@dp.message(Command("leave"), F.chat.type.in_(GROUP))
async def cmd_leave(m: Message):
    g = games.get(m.chat.id)
    if g and g.state == "reg" and m.from_user and g.players.pop(m.from_user.id, None):
        await m.reply(f"Ro'yxatdan chiqdingiz. Qoldi: {len(g.players)}")


@dp.message(Command("stop"), F.chat.type.in_(GROUP))
async def cmd_stop(m: Message):
    g = games.get(m.chat.id)
    if not g or not m.from_user:
        return
    if m.from_user.id != g.owner and not await is_admin(m.chat.id, m.from_user.id):
        return await m.reply("O'yinni faqat boshlagan odam yoki admin to'xtata oladi.")
    if g.task:
        g.task.cancel()
    games.pop(m.chat.id, None)
    await m.answer("🛑 O'yin to'xtatildi.")


@dp.message(Command("profile"))
async def cmd_profile(m: Message):
    if not m.from_user:
        return
    u = user(m.from_user.id, m.from_user.full_name)
    lose = u["games"] - u["wins"]
    rate = round(u["wins"] * 100 / u["games"]) if u["games"] else 0
    await m.answer(
        f"👤 <b>{html.escape(m.from_user.full_name)}</b>\n"
        f"🏅 Daraja: {u['games'] // 10 + 1}\n"
        f"🎮 O'yinlar: {u['games']}\n🏆 G'alaba: {u['wins']} ({rate}%)\n❌ Mag'lubiyat: {lose}\n\n"
        f"💰 Coin: {u['coins']}\n💎 Almaz: {u['diamonds']}\n"
        f"🛡 Himoya: {u['shield']}  📄 Soxta hujjat: {u['fake']}"
        + (f"\n🎴 Rol kartasi: {ROLES[u['pending_role']][0]}" if u["pending_role"] else ""))


@dp.message(Command("top"))
async def cmd_top(m: Message):
    rows = db.execute(
        "SELECT name, wins, games FROM users ORDER BY wins DESC, games ASC LIMIT 10").fetchall()
    if not rows or rows[0]["games"] == 0:
        return await m.answer("Hali reyting yo'q.")
    medals = ["🥇", "🥈", "🥉"]
    text = "🏆 <b>Top 10</b>\n\n" + "\n".join(
        f"{medals[i] if i < 3 else str(i + 1) + '.'} {html.escape(r['name'] or '?')} — "
        f"{r['wins']} g'alaba / {r['games']} o'yin" for i, r in enumerate(rows))
    await m.answer(text)


@dp.message(Command("bonus"))
async def cmd_bonus(m: Message):
    if not m.from_user:
        return
    u = user(m.from_user.id, m.from_user.full_name)
    now = int(time.time())
    left = u["last_bonus"] + 86400 - now
    if left > 0:
        return await m.answer(f"⏳ Keyingi bonus: {left // 3600} soat {left % 3600 // 60} daqiqadan keyin.")
    add(m.from_user.id, coins=50, diamonds=1)
    upd(m.from_user.id, last_bonus=now)
    await m.answer("🎁 Kunlik bonus: +50 💰 va +1 💎")


# ---------------- Do'kon ----------------

ITEMS = {
    "shield": ("🛡 Himoya (1 o'yin, bir marta o'limdan saqlaydi)", "coins", 50),
    "fake": ("📄 Soxta hujjat (1 o'yin, komissar sizni tinch deb ko'radi)", "coins", 60),
    "r_don": ("🎴 Don kartasi (keyingi o'yin)", "diamonds", 5),
    "r_lawyer": ("🎴 Advokat kartasi (keyingi o'yin)", "diamonds", 4),
    "r_doctor": ("🎴 Doktor kartasi (keyingi o'yin)", "diamonds", 3),
    "r_commissar": ("🎴 Komissar kartasi (keyingi o'yin)", "diamonds", 4),
    "r_maniac": ("🎴 Manyak kartasi (keyingi o'yin)", "diamonds", 5),
    "ex": ("💱 200 💰 → 1 💎", "coins", 200),
}


def shop_kb():
    kb = InlineKeyboardBuilder()
    for k, (title, cur, price) in ITEMS.items():
        sym = "💰" if cur == "coins" else "💎"
        kb.row(InlineKeyboardButton(text=f"{title} — {price}{sym}", callback_data=f"b:{k}"))
    return kb.as_markup()


@dp.message(Command("shop"))
async def cmd_shop(m: Message):
    if not m.from_user:
        return
    u = user(m.from_user.id, m.from_user.full_name)
    await m.answer(
        f"🛒 <b>Do'kon</b>\n💰 {u['coins']}   💎 {u['diamonds']}\n\n"
        "Rol kartasi keyingi o'yinda sizga shu rolni beradi (agar o'yinda bunday rol bo'lsa).",
        reply_markup=shop_kb())


@dp.callback_query(F.data.startswith("b:"))
async def cb_buy(c: CallbackQuery):
    key = c.data[2:]
    if key not in ITEMS:
        return await c.answer()
    uid = c.from_user.id
    u = user(uid, c.from_user.full_name)
    title, cur, price = ITEMS[key]
    if u[cur] < price:
        return await c.answer("Mablag' yetarli emas.", show_alert=True)
    if key.startswith("r_"):
        if u["pending_role"]:
            return await c.answer("Sizda allaqachon rol kartasi bor.", show_alert=True)
        upd(uid, pending_role=key[2:])
    elif key == "ex":
        add(uid, diamonds=1)
    elif key == "shield":
        add(uid, shield=1)
    elif key == "fake":
        add(uid, fake=1)
    add(uid, **{cur: -price})
    u = user(uid)
    await c.answer("✅ Sotib olindi!", show_alert=True)
    await safe_edit(
        c.message,
        f"🛒 <b>Do'kon</b>\n💰 {u['coins']}   💎 {u['diamonds']}\n\n"
        "Rol kartasi keyingi o'yinda sizga shu rolni beradi (agar o'yinda bunday rol bo'lsa).",
        shop_kb())


# ---------------- Sozlamalar ----------------

CYCLES = {"reg": [30, 60, 90, 120], "night": [30, 40, 60, 90], "day": [30, 45, 60, 90]}
TOGGLES = ("maniac", "lawyer", "don")


async def is_admin(chat_id: int, uid: int) -> bool:
    try:
        mem = await bot.get_chat_member(chat_id, uid)
        return mem.status in ("creator", "administrator")
    except Exception:
        return False


def settings_kb(chat_id: int):
    c = cfg(chat_id)
    on = lambda k: "✅" if c[k] else "❌"
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text=f"⏱ Ro'yxat: {c['reg']}s", callback_data="s:reg"))
    kb.row(InlineKeyboardButton(text=f"🌙 Tun: {c['night']}s", callback_data="s:night"))
    kb.row(InlineKeyboardButton(text=f"☀️ Kun: {c['day']}s", callback_data="s:day"))
    kb.row(InlineKeyboardButton(text=f"🔪 Manyak: {on('maniac')}", callback_data="s:maniac"))
    kb.row(InlineKeyboardButton(text=f"⚖️ Advokat: {on('lawyer')}", callback_data="s:lawyer"))
    kb.row(InlineKeyboardButton(text=f"🎩 Don: {on('don')}", callback_data="s:don"))
    return kb.as_markup()


@dp.message(Command("settings"), F.chat.type.in_(GROUP))
async def cmd_settings(m: Message):
    if not m.from_user or not await is_admin(m.chat.id, m.from_user.id):
        return await m.reply("Faqat adminlar.")
    await m.answer("⚙️ <b>Sozlamalar</b> (bosib o'zgartiring)", reply_markup=settings_kb(m.chat.id))


@dp.callback_query(F.data.startswith("s:"))
async def cb_settings(c: CallbackQuery):
    chat_id = c.message.chat.id
    if not await is_admin(chat_id, c.from_user.id):
        return await c.answer("Faqat adminlar.", show_alert=True)
    key = c.data[2:]
    cur = cfg(chat_id)
    if key in CYCLES:
        opts = CYCLES[key]
        new = opts[(opts.index(cur[key]) + 1) % len(opts)] if cur[key] in opts else opts[0]
    elif key in TOGGLES:
        new = 0 if cur[key] else 1
    else:
        return await c.answer()
    db.execute(f"UPDATE chats SET {key}=? WHERE id=?", (new, chat_id))
    db.commit()
    await safe_edit(c.message, "⚙️ <b>Sozlamalar</b> (bosib o'zgartiring)", settings_kb(chat_id))
    await c.answer()


# ---------------- O'yin tugmalari ----------------

@dp.callback_query(F.data.startswith(("n:", "p:")))
async def cb_night(c: CallbackQuery):
    kind, chat_id, target = c.data.split(":")
    g = games.get(int(chat_id))
    uid, target = c.from_user.id, int(target)
    if (not g or g.state != "night" or uid not in g.players
            or not g.players[uid]["alive"] or target not in g.players
            or not g.players[target]["alive"]):
        return await c.answer("Hozir mumkin emas.", show_alert=True)
    role = g.players[uid]["role"]
    if kind == "p":
        if role != "lawyer":
            return await c.answer()
        g.protect = target
    elif role in MAFIA_TEAM:
        g.mafia_votes[uid] = target
    elif role == "doctor":
        if target == g.last_heal:
            return await c.answer("Ketma-ket bir odamni emas.", show_alert=True)
        g.heal = target
    elif role == "maniac":
        g.maniac_target = target
    elif role == "commissar":
        if g.check:
            return await c.answer("Bu kecha tekshirib bo'ldingiz.", show_alert=True)
        g.check = (uid, target)
    else:
        return await c.answer()
    await safe_edit(c.message, f"Tanlandi: {html.escape(g.players[target]['name'])}")
    await c.answer()


@dp.callback_query(F.data.startswith("v:"))
async def cb_vote(c: CallbackQuery):
    _, chat_id, target = c.data.split(":")
    g = games.get(int(chat_id))
    uid, target = c.from_user.id, int(target)
    if (not g or g.state != "day" or uid not in g.players
            or not g.players[uid]["alive"]):
        return await c.answer("Siz ovoz bera olmaysiz.", show_alert=True)
    if target and (target not in g.players or not g.players[target]["alive"]):
        return await c.answer()
    g.votes[uid] = target
    await c.answer("Ovoz qabul qilindi ✅")


@dp.error()
async def on_error(event):
    logging.error("Update xatosi: %s", event.exception, exc_info=event.exception)
    return True


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not bot:
        raise SystemExit("BOT_TOKEN o'rnatilmagan.")
    while True:
        try:
            await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
            break
        except Exception:
            logging.exception("Polling to'xtadi, 5 soniyadan keyin qayta ishga tushadi")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())

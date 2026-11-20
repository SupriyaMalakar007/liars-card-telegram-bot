import os
import random
import logging
import asyncio
import html
import json
from dataclasses import dataclass, field
from typing import Optional
from telegram.error import TelegramError, RetryAfter

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    AIORateLimiter,
)

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
USER_FILE = "users.json"


def load_users():
    try:
        with open(USER_FILE, "r", encoding="utf-8") as file:
            return {int(uid) for uid in json.load(file)}
    except FileNotFoundError:
        return set()
    except (json.JSONDecodeError, TypeError, ValueError):
        logging.exception("Could not read users.json")
        return set()


def save_users(users):
    with open(USER_FILE, "w", encoding="utf-8") as file:
        json.dump(sorted(users), file, indent=2)


KNOWN_USERS = load_users()


def register_user(user_id):
    if user_id not in KNOWN_USERS:
        KNOWN_USERS.add(user_id)
        save_users(KNOWN_USERS)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
async def safe_send_message(context, **kwargs):
    """Send without a global 1.1-second queue; AIORateLimiter handles pacing."""
    for attempt in range(4):
        try:
            return await context.bot.send_message(**kwargs)

        except RetryAfter as error:
            wait_time = error.retry_after + 0.5
            logging.warning(
                "Telegram flood control. Waiting %s seconds.",
                wait_time,
            )
            await asyncio.sleep(wait_time)

        except TelegramError as error:
            if attempt == 3:
                logging.error(
                    "Message failed after 4 attempts: %s",
                    error,
                )
                return None

            wait_time = 0.5 * (2 ** attempt)
            logging.warning(
                "Telegram error: %s. Retrying in %s seconds.",
                error,
                wait_time,
            )
            await asyncio.sleep(wait_time)

    return None


RANKS = ["Ace", "King", "Queen"]
MAX_PLAYERS = 4
CARDS_PER_PLAYER = 5
MAX_CARDS_PER_TURN = 3


@dataclass
class Game:
    chat_id: int
    players: list[int] = field(default_factory=list)
    ai_players: set[int] = field(default_factory=set)
    names: dict[int, str] = field(default_factory=dict)
    hands: dict[int, list[str]] = field(default_factory=dict)
    alive: set[int] = field(default_factory=set)
    reserve: list[str] = field(default_factory=list)
    pile: list[str] = field(default_factory=list)
    current_player: Optional[int] = None
    latest_player: Optional[int] = None
    latest_cards: list[str] = field(default_factory=list)
    latest_claim: Optional[str] = None
    selected: dict[int, set[int]] = field(default_factory=dict)
    phase: str = "lobby"
    round_number: int = 0
    table_rank: Optional[str] = None
    # These values are created once at game start and never reset between rounds.
    bullet_positions: dict[int, int] = field(default_factory=dict)
    shots_taken: dict[int, int] = field(default_factory=dict)
    timeout_strikes: dict[int, int] = field(default_factory=dict)
    turn_timeout_task: Optional[asyncio.Task] = None
    turn_token: int = 0


GAMES: dict[int, Game] = {}


def create_deck() -> list[str]:
    """Create the exact 21-card deck: 6 each of three ranks, 2 Jokers, 1 Devil."""
    deck = (
        ["King"] * 6
        + ["Queen"] * 6
        + ["Ace"] * 6
        + ["Joker"] * 2
        + ["DEVIL"]
    )
    assert len(deck) == 21
    return deck


def active_players(game: Game) -> list[int]:
    return [uid for uid in game.players if uid in game.alive]


def players_with_cards(game: Game) -> list[int]:
    return [uid for uid in active_players(game) if game.hands.get(uid)]


def player_name(game: Game, uid: Optional[int]) -> str:
    if uid is None:
        return "Unknown player"
    return game.names.get(uid, str(uid))


def next_player(game: Game, after_user: Optional[int]) -> Optional[int]:
    alive = active_players(game)
    if not alive:
        return None
    if after_user not in game.players:
        return alive[0]
    start_index = game.players.index(after_user)
    for step in range(1, len(game.players) + 1):
        candidate = game.players[(start_index + step) % len(game.players)]
        if candidate in game.alive:
            return candidate
    return None


def next_player_with_cards(game: Game, after_user: Optional[int]) -> Optional[int]:
    """Skip living players who have already used all cards in this round."""
    holders = set(players_with_cards(game))
    if not holders:
        return None
    if after_user not in game.players:
        return next((uid for uid in game.players if uid in holders), None)
    start_index = game.players.index(after_user)
    for step in range(1, len(game.players) + 1):
        candidate = game.players[(start_index + step) % len(game.players)]
        if candidate in holders:
            return candidate
    return None


def get_status(game: Game) -> str:
    lines = [
        "🎭 LIAR'S CARD GAME",
        f"Round: {game.round_number}",
        f"Table Rank: {game.table_rank or 'Not selected'}",
        f"Players alive: {len(game.alive)}",
        "",
    ]
    for uid in active_players(game):
        lines.append(
            f"• {player_name(game, uid)}: {len(game.hands.get(uid, []))} cards"
        )
        if uid in game.shots_taken:
            lines.append(
                f"  Roulette: {game.shots_taken[uid]}/6"
            )
    if game.latest_player is not None:
        lines.extend([
            "",
            f"Latest player: {player_name(game, game.latest_player)}",
            f"Claim: {len(game.latest_cards)} {game.latest_claim}(s)",
        ])
    if game.current_player is not None:
        lines.extend(["", f"Current turn: {player_name(game, game.current_player)}"])
    return "\n".join(lines)



async def send_hand(
    context: ContextTypes.DEFAULT_TYPE,
    game: Game,
    uid: int
) -> None:
    if uid in game.ai_players:
        return

    cards = game.hands.get(uid, [])

    if not cards:
        message = "Your hand is empty. Wait for the next round."
    else:
        message = "🃏 YOUR CARDS\n\n" + "\n".join(
            f"{i + 1}. {card}"
            for i, card in enumerate(cards)
        )

    result = await safe_send_message(
        context,
        chat_id=uid,
        text=message,
    )

    if result is None:
        await safe_send_message(
            context,
            chat_id=game.chat_id,
            text=(
                f"⚠️ {player_name(game, uid)}: Open the bot privately "
                "and send /start first so I can send your cards."
            ),
        )




async def finish_game(context: ContextTypes.DEFAULT_TYPE, game: Game) -> None:
    cancel_turn_timeout(game)
    alive = active_players(game)

    if len(alive) == 1:
        winner = alive[0]
        await safe_send_message(
            context,
            chat_id=game.chat_id,
            text=(
                f"🏆 {player_name(game, winner)} IS THE LAST PLAYER STANDING!\n\n"
                "🎉 Congratulations! Game Over!"
            ),
        )

    elif len(alive) == 0:
        await safe_send_message(
            context,
            chat_id=game.chat_id,
            text="The game has ended with no surviving players.",
        )

    game.phase = "finished"
    game.current_player = None



async def start_new_round(
    context: ContextTypes.DEFAULT_TYPE,
    game: Game,
    starter: Optional[int],
    show_first_turn: bool = True,
) -> None:
    """Shuffle a fresh deck, deal five to every survivor, and begin at starter."""
    cancel_turn_timeout(game)
    alive = active_players(game)
    if len(alive) <= 1:
        await finish_game(context, game)
        return

    deck = create_deck()
    random.shuffle(deck)

    game.hands = {uid: [] for uid in game.players}
    for uid in alive:
        game.hands[uid] = [deck.pop() for _ in range(CARDS_PER_PLAYER)]

    game.reserve = deck
    game.pile = []
    game.latest_player = None
    game.latest_cards = []
    game.latest_claim = None
    game.selected.clear()
    game.round_number += 1
    game.table_rank = random.choice(RANKS)

    if starter not in game.alive:
        starter = next_player(game, starter)
    if starter not in game.alive:
        starter = alive[0]
    game.current_player = starter

    await safe_send_message(
        context,
        chat_id=game.chat_id,
        text=(
            f"🔄 ROUND {game.round_number} STARTED!\n\n"
            f"🎴 New shuffle: every living player receives {CARDS_PER_PLAYER} cards.\n"
            f"👑 TABLE RANK: {game.table_rank.upper()}\n"
            f"➡️ First turn: {player_name(game, starter)}\n"
            "Only the Table Rank and Jokers are valid for this table. "
            "DEVIL may be played at any table."
        ),
    )

    for uid in alive:
        await send_hand(context, game, uid)
    if show_first_turn:
     await show_turn(context, game)

def cancel_turn_timeout(game: Game) -> None:
    game.turn_token += 1

    if game.turn_timeout_task and not game.turn_timeout_task.done():
        game.turn_timeout_task.cancel()

    game.turn_timeout_task = None


async def turn_timeout(
    context: ContextTypes.DEFAULT_TYPE,
    game: Game,
    uid: int,
    token: int,
) -> None:
    try:
        await asyncio.sleep(45)

        # Ignore an expired timer if the turn has already changed.
        if (
            game.phase != "playing"
            or game.current_player != uid
            or game.turn_token != token
            or uid not in game.alive
            or uid in game.ai_players
        ):
            return

        strikes = game.timeout_strikes.get(uid, 0) + 1
        game.timeout_strikes[uid] = strikes

        if strikes >= 2:
            await safe_send_message(
                context,
                chat_id=game.chat_id,
                text=(
                    f"🚫 {player_name(game, uid)} missed 2 consecutive turns!\n"
                    "💥 PLAYER ELIMINATED!"
                ),
            )
            await eliminate(context, game, uid)

            if len(game.alive) <= 1:
                game.turn_timeout_task = None
                await finish_game(context, game)
                return
        else:
            await safe_send_message(
                context,
                chat_id=game.chat_id,
                text=(
                    f"⏰ TIME'S UP!\n"
                    f"{player_name(game, uid)} missed their turn.\n"
                    "⚠️ Warning 1/2.\n"
                    "⏭️ Turn skipped. One more consecutive timeout "
                    "will eliminate this player."
                ),
            )

        # The 45-second window to challenge the previous play has expired.
        game.latest_player = None
        game.latest_cards = []
        game.latest_claim = None

        game.current_player = next_player_with_cards(game, uid)
        game.turn_timeout_task = None
        await show_turn(context, game)

    except asyncio.CancelledError:
        pass


async def show_turn(context: ContextTypes.DEFAULT_TYPE, game: Game) -> None:
    while True:
        if len(game.alive) <= 1:
            await finish_game(context, game)
            return

        if game.phase != "playing":
            return

        if (
            game.current_player not in game.alive
            or not game.hands.get(game.current_player)
        ):
            game.current_player = next_player_with_cards(
                game, game.current_player
            )

        # If everyone has emptied their hand, start a fresh round.
        if game.current_player is None:
            starter = active_players(game)[0]
            await start_new_round(
                context, game, starter, show_first_turn=False
            )
            continue

        uid = game.current_player

        # Start exactly one 45-second timer for each human turn.
        cancel_turn_timeout(game)
        if uid not in game.ai_players:
            token = game.turn_token
            game.turn_timeout_task = asyncio.create_task(
                turn_timeout(context, game, uid, token)
            )

        # Process AI turns in this loop, not through recursive calls.
        if uid in game.ai_players:
            await ai_take_turn(context, game, uid)

            if game.phase == "finished":
                return

            continue

        buttons = [
            [InlineKeyboardButton("🃏 Play 1–3 Cards", callback_data="play")],
            [InlineKeyboardButton("✋ LIAR (next player only)", callback_data="liar")],
            [InlineKeyboardButton("📊 Status", callback_data="status")],
        ]

        tagged_name = html.escape(player_name(game, uid))
        mention = f'<a href="tg://user?id={uid}">{tagged_name}</a>'

        await safe_send_message(
            context,
            chat_id=game.chat_id,
            text=(
                f"👑 TABLE RANK: {game.table_rank.upper()}\n"
                f"➡️ {mention}, IT'S YOUR TURN! 🎴\n"
                "Please play your cards now."
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return


async def submit_play(
    context: ContextTypes.DEFAULT_TYPE,
    game: Game,
    uid: int,
    indices: list[int],
    advance_turn: bool = True,
) -> None:
    if game.current_player != uid:
        return

    indices = sorted(indices)
    hand = game.hands.get(uid, [])

    if not 1 <= len(indices) <= MAX_CARDS_PER_TURN:
        return

    if any(i < 0 or i >= len(hand) for i in indices):
        return

    # A valid play cancels the timer and resets consecutive timeout strikes.
    cancel_turn_timeout(game)
    game.timeout_strikes[uid] = 0

    played = [hand[i] for i in indices]
    claim = game.table_rank

    await safe_send_message(
        context,
        chat_id=game.chat_id,
        text=(
            f"🃏 {player_name(game, uid)} played {len(played)} card(s).\n"
            f"Table Rank: {game.table_rank}\n"
            f"Claim: {len(played)} {claim}(s)."
        ),
    )

    for i in reversed(indices):
        hand.pop(i)

    game.pile.extend(played)
    game.latest_player = uid
    game.latest_cards = played
    game.latest_claim = claim
    game.selected.pop(uid, None)

    if uid not in game.ai_players:
        try:
            await safe_send_message(
                context,
                chat_id=uid,
                text=(
                    "✅ DONE!\n\n"
                    "Your claim was successfully submitted to the group.\n"
                    "Go back to the group and wait for the next turn."
                ),
            )
        except TelegramError:
            logging.exception("Could not send private confirmation.")

    other_holders = [
        player for player in active_players(game)
        if player != uid and game.hands.get(player)
    ]

    if len(hand) == 0 and len(other_holders) == 1:
        await safe_send_message(
            context,
            chat_id=game.chat_id,
            text=(
                "⚠️ ONLY TWO PLAYERS HAD CARDS!\n"
                "🤖 AUTOMATIC LIAR CHALLENGE!"
            ),
        )

        await resolve_challenge(
                context,
                game,
                challenger=other_holders[0],
                automatic=True,
                show_first_turn=advance_turn,
        )
        return

    game.current_player = next_player_with_cards(game, uid)

    if game.current_player is None:
     starter = next_player(game, uid)
     await start_new_round(
        context, game, starter, show_first_turn=advance_turn
     )
     return

    if advance_turn:
     await show_turn(context, game)


async def ai_take_turn(
    context: ContextTypes.DEFAULT_TYPE,
    game: Game,
    uid: int,
) -> None:

    if uid not in game.alive or not game.hands.get(uid):
        game.current_player = next_player_with_cards(game, uid)
        return

    # AI can challenge the previous player's claim.
    if (
        game.latest_player is not None
        and next_player_with_cards(game, game.latest_player) == uid
        and random.random() < 0.30
    ):
        await safe_send_message(
            context,
            chat_id=game.chat_id,
            text=f"🤖 {player_name(game, uid)} calls LIAR!",
        )

        await resolve_challenge(
            context,
            game,
            challenger=uid,
            show_first_turn=False,
        )
        return

    hand = game.hands[uid]
    count = random.randint(1, min(3, len(hand)))

    valid_indices = [
        i for i, card in enumerate(hand)
        if card in (game.table_rank, "Joker")
    ]

    # Usually play valid cards, but sometimes bluff.
    if valid_indices and random.random() >= 0.20:
        chosen = random.sample(
            valid_indices,
            min(count, len(valid_indices)),
        )

        remaining = [
            i for i in range(len(hand))
            if i not in chosen
        ]

        while len(chosen) < count and remaining:
            extra = random.choice(remaining)
            chosen.append(extra)
            remaining.remove(extra)
    else:
        chosen = random.sample(range(len(hand)), count)

    

    await submit_play(
        context,
        game,
        uid,
        chosen,
        advance_turn=False,
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    # Save user ID for future broadcasts
    if update.effective_user:
        register_user(update.effective_user.id)
    await update.message.reply_text(
        "🎭 Welcome to Liar's Card Game!\n\n"
        "Add me to a Telegram group.\n"
        "Use /newgame to create a game.\n"
        "Use /join to join.\n"
        "Use /addbot to add an AI player to the game.\n"
        "Use /startgame to begin.\n\n"
        "Use /hand privately to see your cards."
    )


async def newgame(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text("Please create the game in a Telegram group.")
        return

    old = GAMES.get(chat.id)
    if old and old.phase in ("lobby", "playing"):
        await update.message.reply_text("A game is already running. Use /cancelgame first.")
        return

    GAMES[chat.id] = Game(chat_id=chat.id)
    await update.message.reply_text(
        "🎮 NEW GAME CREATED!\n\nMinimum players: 2\nMaximum players: 4\n\nPlayers, use /join."
    )


async def join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    game = GAMES.get(chat_id)
    if not game or game.phase != "lobby":
        await update.message.reply_text("No open lobby. Use /newgame.")
        return

    uid = update.effective_user.id
    if uid in game.players:
        await update.message.reply_text("You already joined.")
        return
    if len(game.players) >= MAX_PLAYERS:
        await update.message.reply_text("Maximum 4 players allowed.")
        return

    game.players.append(uid)
    game.names[uid] = update.effective_user.full_name
    await update.message.reply_text(
        f"✅ {player_name(game, uid)} joined!\nPlayers: {len(game.players)}/{MAX_PLAYERS}"
    )


async def addbot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    game = GAMES.get(chat_id)

    if not game or game.phase != "lobby":
        await update.message.reply_text("No open lobby. Use /newgame.")
        return

    if len(game.players) >= MAX_PLAYERS:
        await update.message.reply_text("Maximum 4 players allowed.")
        return

    bot_number = len(game.ai_players) + 1
    bot_id = -(bot_number)

    while bot_id in game.players:
        bot_number += 1
        bot_id = -bot_number

    game.players.append(bot_id)
    game.ai_players.add(bot_id)
    game.names[bot_id] = f"AI Player {bot_number}"

    await update.message.reply_text(
        f"🤖 {game.names[bot_id]} joined the game!\n"
        f"Players: {len(game.players)}/{MAX_PLAYERS}"
    )

async def startgame(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    game = GAMES.get(update.effective_chat.id)
    if not game or game.phase != "lobby":
        await update.message.reply_text("Create a lobby first.")
        return
    if len(game.players) < 2:
        await update.message.reply_text("At least 2 players are required.")
        return

    game.alive = set(game.players)
    # A fixed bullet chamber and a separate 0..6 counter for each player.
    # These are intentionally not reset when a new round starts.
    game.bullet_positions = {uid: random.randint(1, 6) for uid in game.players}
    game.shots_taken = {uid: 0 for uid in game.players}
    game.phase = "playing"

    await update.message.reply_text(
        "🎮 GAME STARTED!\n\n"
        "Each player receives 5 cards per round.\n"
        "Play 1, 2 or 3 cards per turn.\n"
        "Jokers are wild. Devil has a special rule.\n"
        "Russian Roulette uses a fixed bullet chamber for each player.\n\n"
        "Open the bot privately and press /start to receive your cards."
    )
    await start_new_round(context, game, game.players[0])


async def hand_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if update.effective_chat.type != "private":
        await update.message.reply_text("Send /hand in the bot's private chat.")
        return
    for game in GAMES.values():
        if uid in game.players and game.phase == "playing":
            if uid not in game.alive:
                await update.message.reply_text("You have been eliminated from this game.")
            else:
                await send_hand(context, game, uid)
            return
    await update.message.reply_text("You are not in an active game.")


async def cancelgame(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    game = GAMES.pop(update.effective_chat.id, None)
    if game:
        cancel_turn_timeout(game)
    await update.message.reply_text("Game cancelled." if game else "No active game.")


async def eliminate(context: ContextTypes.DEFAULT_TYPE, game: Game, uid: int) -> None:
    if uid not in game.alive:
        return
    game.alive.remove(uid)
    game.pile.extend(game.hands.get(uid, []))
    game.hands[uid] = []
    game.selected.pop(uid, None)
    await safe_send_message(
        context,
        chat_id=game.chat_id,
        text=f"💥 {player_name(game, uid)} HAS BEEN ELIMINATED FROM THE GAME!",
    )


async def russian_roulette(
    context: ContextTypes.DEFAULT_TYPE,
    game: Game,
    targets: list[int],
) -> Optional[int]:
    """Each target takes exactly their next sequential shot; return first target."""
    first_target = next((uid for uid in targets if uid in game.alive), None)

    for uid in targets:
        if uid not in game.alive:
            continue

        previous = game.shots_taken.get(uid, 0)
        # A player who reaches shot six must be eliminated; never allow 7/6.
        if previous >= 6:
            await eliminate(context, game, uid)
            continue

        shot_number = previous + 1
        game.shots_taken[uid] = shot_number
        bullet_position = game.bullet_positions[uid]

        await safe_send_message(
            context,
            chat_id=game.chat_id,
            text=(
                f"🔫 {player_name(game, uid)} pulls the trigger...\n"
                f"Chamber: {shot_number}/6"
            ),
        )

        if shot_number == bullet_position:
            await safe_send_message(context, chat_id=game.chat_id, text="💥 BANG!")
            await eliminate(context, game, uid)
        else:
            await safe_send_message(
                context,
                chat_id=game.chat_id,
                text=(
                    f"😮 CLICK! {player_name(game, uid)} SURVIVES!\n"
                    f"Roulette progress: {shot_number}/6"
                ),
            )

    return first_target


async def resolve_challenge(
    context: ContextTypes.DEFAULT_TYPE,
    game: Game,
    challenger: Optional[int],
    automatic: bool = False,
    show_first_turn: bool = True,
) -> None:
    cancel_turn_timeout(game)
    played_by = game.latest_player
    revealed = list(game.latest_cards)
    claim = game.latest_claim
    if played_by is None or not revealed:
        return

    devil_present = "DEVIL" in revealed
    # A claim is true only if each non-Devil card is the selected table rank or Joker.
    valid = (
        claim == game.table_rank
        and all(card in (game.table_rank, "Joker") for card in revealed if card != "DEVIL")
    )

    await safe_send_message(
        context,
        chat_id=game.chat_id,
        text=(
            "🚨 LIAR CHALLENGE!\n\n"
            f"Player: {player_name(game, played_by)}\n"
            f"Claim: {claim}\n"
            "Revealed cards:\n" + "\n".join(f"• {card}" for card in revealed)
        ),
    )

    if devil_present:
        await safe_send_message(
            context,
            chat_id=game.chat_id,
            text=(
                "😈 DEVIL CARD REVEALED!\n"
                "The Devil player is SAFE. Every other living player must play Russian Roulette."
            ),
        )
        # If there is a challenger, let that challenger be the first Roulette player.
        targets = [uid for uid in active_players(game) if uid != played_by]
        if challenger in targets:
            targets.remove(challenger)
            targets.insert(0, challenger)
    else:
        if automatic:
            # In a two-card-holder endgame, the other player with cards challenges.
            candidates = [uid for uid in players_with_cards(game) if uid != played_by]
            challenger = candidates[0] if candidates else next_player(game, played_by)

        if valid:
            loser = challenger
            await safe_send_message(
                context,
                chat_id=game.chat_id,
                text=(
                    "✅ CLAIM IS TRUE!\n"
                    f"{player_name(game, loser)} must play Russian Roulette."
                ),
            )
        else:
            loser = played_by
            await safe_send_message(
                context,
                chat_id=game.chat_id,
                text=(
                    "❌ CLAIM IS FALSE!\n"
                    f"{player_name(game, loser)} must play Russian Roulette."
                ),
            )
        targets = [loser] if loser in game.alive else []

    # Capture Roulette order before clearing the latest play. The first Roulette
    # participant starts the next round if alive; otherwise the next living player does.
    round_starter = next((uid for uid in targets if uid in game.alive), None)
    game.latest_player = None
    game.latest_cards = []
    game.latest_claim = None
    game.current_player = None

    await russian_roulette(context, game, targets)

    if len(game.alive) <= 1:
        await finish_game(context, game)
        return

    # Prefer the first Roulette participant who survived. This also handles
    # Devil, where several players may take a shot in the same challenge.
    surviving_roulette_player = next(
        (uid for uid in targets if uid in game.alive), None
    )
    if surviving_roulette_player is not None:
        round_starter = surviving_roulette_player
    elif round_starter not in game.alive:
        round_starter = next_player(game, round_starter)

    await start_new_round(
    context,
    game,
    round_starter,
    show_first_turn=show_first_turn,
)


async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rules_text = """🎭 LIAR'S CARD GAME — RULES

👥 PLAYERS
• 2–4 players.

🃏 DECK & ROUNDS
• 6 Kings, 6 Queens, 6 Aces, 2 Jokers and 1 Devil (21 cards).
• Every living player gets 5 cards in every round.
• The deck is shuffled again at the start of every round.
• A random Table Rank (Ace, King or Queen) is selected every round.

🎮 PLAYING
• Play 1–3 cards per turn.
• Each human player has 45 seconds to complete their turn.
• First timeout: turn is skipped and the player gets a warning.
• Two consecutive timeouts: player is eliminated.
• A successful card play resets the timeout count.
• Choose a claim: Ace, King or Queen.
• The Table Rank and Jokers make a true claim; other ranks are false.
• Devil can be played in any table and can be mixed with other cards.

✋ LIAR
• Players can challenge the latest play, except their own.
• Only the latest played cards are revealed.
• True claim: challenger plays Roulette.
• False claim: player who played the cards plays Roulette.
• Automatic LIAR happens only when exactly two living players still have cards and one of them plays their last card; the other card-holding player challenges.

😈 DEVIL
• If Devil is revealed after a challenge, Devil player is safe.
• Every other living player plays Russian Roulette.
• If nobody challenges the Devil play, its special effect does not happen.

🔫 RUSSIAN ROULETTE
• Each player's bullet position (1–6) is chosen once at game start.
• Each player has an independent shot counter: 1/6, 2/6 ... 6/6.
• The counter and bullet position never reset between rounds.
• The bullet is hit on that player's fixed bullet-number shot. No 7/6 is possible.
• A player is eliminated only when hit by the bullet.
• After Roulette, the current round ends and a new round begins if at least two players remain.
• The first Roulette participant starts the next round if alive; otherwise the next living player starts.

🏆 WINNER
• The last surviving player wins.
"""
    await update.message.reply_text(rules_text)


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    uid = query.from_user.id
    data = query.data or ""

    # Private card-selection callbacks carry the originating group chat ID.
    if data.startswith(("sel:", "done:", "rank:")):
        parts = data.split(":")
        chat_id = int(parts[1])
        game = GAMES.get(chat_id)
        if not game or game.phase != "playing":
            await query.answer("Game is not active.", show_alert=True)
            return
        if uid not in game.alive or game.current_player != uid:
            await query.answer(
                "It is not your turn (or you are eliminated).",
                show_alert=True,
            )
            return

        if data.startswith("sel:"):
            index = int(parts[2])
            hand = game.hands.get(uid, [])
            if index < 0 or index >= len(hand):
                await query.answer("That card is no longer available.", show_alert=True)
                return

            selected = game.selected.setdefault(uid, set())
            if index in selected:
                selected.remove(index)
            elif len(selected) < MAX_CARDS_PER_TURN:
                selected.add(index)
            else:
                await query.answer("Maximum 3 cards.", show_alert=True)
                return

            keyboard = []
            for i, card in enumerate(hand):
                mark = "✅" if i in selected else "☐"
                keyboard.append([
                    InlineKeyboardButton(
                        f"{mark} {i + 1}. {card}",
                        callback_data=f"sel:{chat_id}:{i}",
                    )
                ])
            keyboard.append([
                InlineKeyboardButton("Continue", callback_data=f"done:{chat_id}")
            ])
            # Acknowledge the tap before editing the keyboard.
            await query.answer()
            await query.edit_message_reply_markup(InlineKeyboardMarkup(keyboard))
            return

        if data.startswith("done:"):
            selected = game.selected.get(uid, set())
            if not 1 <= len(selected) <= MAX_CARDS_PER_TURN:
                await query.answer("Select 1–3 cards.", show_alert=True)
                return

            table_rank = game.table_rank
            if table_rank not in RANKS:
                await query.answer(
                    "Table Rank is not set. Please contact the host.",
                    show_alert=True,
                )
                return

            keyboard = [[
                InlineKeyboardButton(
                    f"👑 {table_rank} (TABLE RANK)",
                    callback_data=f"rank:{chat_id}:{table_rank}",
                )
            ]]
            await query.answer()
            await query.message.reply_text(
                f"Choose your claim. Current Table Rank: {table_rank}",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

        if data.startswith("rank:"):
            claim = parts[2]
            if claim not in RANKS or claim != game.table_rank:
                await query.answer(
                    f"Only the current Table Rank ({game.table_rank}) can be claimed.",
                    show_alert=True,
                )
                return

            indices = sorted(game.selected.get(uid, set()))
            hand = game.hands.get(uid, [])
            if not 1 <= len(indices) <= MAX_CARDS_PER_TURN:
                await query.answer("Select cards first.", show_alert=True)
                return
            if any(i < 0 or i >= len(hand) for i in indices):
                await query.answer(
                    "Your card selection expired. Please select again.",
                    show_alert=True,
                )
                return

            # Acknowledge the tap before running the play/turn logic.
            await query.answer()
            await submit_play(context, game, uid, indices)
            return

    game = GAMES.get(query.message.chat_id)
    if not game or game.phase != "playing":
        await query.answer("No active game.", show_alert=True)
        return

    if data == "status":
        await query.answer()
        await query.message.reply_text(get_status(game))
        return

    if data == "play":
        if game.current_player != uid:
            await query.answer("It is not your turn.", show_alert=True)
            return
        if uid not in game.alive:
            await query.answer("You are eliminated.", show_alert=True)
            return

        keyboard = []
        for i, card in enumerate(game.hands.get(uid, [])):
            keyboard.append([
                InlineKeyboardButton(
                    f"☐ {i + 1}. {card}",
                    callback_data=f"sel:{game.chat_id}:{i}",
                )
            ])
        keyboard.append([
            InlineKeyboardButton("Continue", callback_data=f"done:{game.chat_id}")
        ])

        await query.answer()
        try:
            sent = await safe_send_message(
                context,
                chat_id=uid,
                text="Select 1–3 cards:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            if sent is not None:
                await query.message.reply_text(
                    "I sent your cards to your private chat."
                )
            else:
                await query.message.reply_text(
                    "⚠️ I couldn't send your cards. Open my private chat and press /start first."
                )
        except Exception:
            await query.message.reply_text(
                "⚠️ Open my private chat and press /start first."
            )
        return

    if data == "liar":
        if game.latest_player is None:
            await query.answer("Nothing to challenge.", show_alert=True)
            return
        if uid == game.latest_player:
            await query.answer("You cannot challenge your own play.", show_alert=True)
            return
        if uid not in game.alive:
            await query.answer("You are eliminated.", show_alert=True)
            return

        # Only the next active player who still has cards may challenge.
        allowed_challenger = next_player_with_cards(game, game.latest_player)
        if uid != allowed_challenger:
            await query.answer(
                f"Only {player_name(game, allowed_challenger)} can call LIAR now."
                if allowed_challenger is not None
                else "There is no eligible player to call LIAR.",
                show_alert=True,
            )
            return

        await query.answer()
        game.timeout_strikes[uid] = 0
        await resolve_challenge(context, game, challenger=uid)
        return

    await query.answer()

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    # Only admin can broadcast
    if not update.effective_user or update.effective_user.id != ADMIN_ID or ADMIN_ID == 0:
        await update.message.reply_text(
            "⛔ You are not authorized to use this command."
        )
        return

    message = " ".join(context.args).strip()

    if not message:
        await update.message.reply_text(
            "Usage:\n/broadcast Your update message here"
        )
        return

    if not KNOWN_USERS:
        await update.message.reply_text(
            "No registered users found."
        )
        return

    sent = 0
    failed = 0

    for user_id in list(KNOWN_USERS):
        try:
            result = await safe_send_message(
                context,
                chat_id=user_id,
                text=f"📢 BOT UPDATE\n\n{message}"
            )

            if result is not None:
             sent += 1
            else:
             failed += 1

        except TelegramError:
            failed += 1
            logging.exception(
                "Broadcast failed for user %s", user_id
            )

    await update.message.reply_text(
        "✅ BROADCAST COMPLETED!\n\n"
        f"📨 Successfully sent: {sent}\n"
        f"⚠️ Failed: {failed}\n"
        f"👥 Total users: {len(KNOWN_USERS)}"
    )
async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
) -> None:
    logging.error(
        "Unhandled exception while processing update:",
        exc_info=context.error,
    )


def main() -> None:
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is missing. Add it to your .env file.")

    app = (
        Application.builder()
        .token(TOKEN)
        .rate_limiter(
            AIORateLimiter(
                group_max_rate=18,
                group_time_period=60,
                max_retries=3,
            )
        )
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rules", rules))
    app.add_handler(CommandHandler("newgame", newgame))
    app.add_handler(CommandHandler("join", join))
    app.add_handler(CommandHandler("addbot", addbot))
    app.add_handler(CommandHandler("startgame", startgame))
    app.add_handler(CommandHandler("hand", hand_command))
    app.add_handler(CommandHandler("cancelgame", cancelgame))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CallbackQueryHandler(callback))

    app.add_error_handler(error_handler)

    print("Liar's Card Game Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()

# Liar's Card Game Telegram Bot

A multiplayer Telegram card game for 2–4 players.

## Features

- 2–4 players
- 21-card deck
- 5 cards per player
- Play 1, 2 or 3 cards per turn
- Ace, King and Queen claims
- 2 Wild Jokers
- Special Devil Card
- LIAR challenge
- Automatic LIAR when a player plays their last card
- Six-chamber Russian Roulette
- Last Player Standing wins

## Card Deck

| Card | Quantity |
|---|---:|
| Ace | 6 |
| King | 6 |
| Queen | 6 |
| Joker | 2 |
| Devil | 1 |
| Total | 21 |

## Game Rules

### 1. Players
Minimum 2 and maximum 4 players.

### 2. Cards
Each player receives 5 cards.
Remaining cards stay hidden.

### 3. Playing
A player can play 1, 2 or 3 cards at once.
The player announces the same number of Ace, King or Queen.

### 4. Joker
Jokers are wild cards.
They can count as Ace, King or Queen.
Players cannot announce Joker as a claim.

### 5. LIAR
Any other living player can challenge the latest play.
Only the latest played cards are revealed.

- True claim: Challenger plays Russian Roulette.
- False claim: Player who played the cards plays Russian Roulette.

### 6. Automatic LIAR
When a player plays their last card, the bot automatically challenges that play.

For a normal automatic challenge, this version treats the next living player as the challenger.

### 7. Devil Card
Devil can be played with other cards.
It can be played regardless of the current claimed rank.

If a challenged play reveals at least one Devil:
- The player who played Devil is safe.
- Every other living player plays Russian Roulette.
- This overrides the normal challenge result.

If Devil is not challenged, its special effect does not activate.

### 8. Russian Roulette
There are six chambers and one bullet.

- Empty chamber: player survives and continues playing.
- Bullet chamber: player is eliminated.

Each player's bullet position is independently randomized for each shot.

### 9. Winner
The last surviving player wins.

## Setup on Windows

### Step 1: Install Python

Install Python 3.10 or newer.

### Step 2: Open the project

Open this folder in VS Code:

liars_card_telegram_bot

### Step 3: Create virtual environment

Open PowerShell in VS Code:

```powershell
py -m venv .venv
```

### Step 4: Activate environment

```powershell
.\.venv\Scripts\Activate.ps1
```

### Step 5: Install libraries

```powershell
pip install -r requirements.txt
```

### Step 6: Create Telegram bot

1. Open Telegram.
2. Search for @BotFather.
3. Send /newbot.
4. Follow the instructions.
5. Copy the new bot token.

### Step 7: Create .env

Make a copy of `.env.example`.
Rename the copy to `.env`.

Put your token in it:

```text
BOT_TOKEN=YOUR_REAL_BOT_TOKEN
```

Do not share this file or upload it to GitHub.

### Step 8: Run the bot

```powershell
python bot.py
```

You should see:

Liar's Card Game Bot is running...

## How to Play

1. Add the bot to a Telegram group.
2. Every player opens the bot privately and presses Start.
3. In the group, send `/newgame`.
4. Every player sends `/join`.
5. Send `/startgame`.
6. The bot privately sends each player's cards.
7. The current player presses Play 1–3 Cards.
8. Select cards privately and choose a claim.
9. Other players can press LIAR.
10. The bot reveals the latest play and resolves the challenge.

## Commands

| Command | Description |
|---|---|
| /start | Start the bot |
| /newgame | Create a new game |
| /join | Join a game |
| /startgame | Start the game |
| /rules | Rules of the game |
| /hand | Show your private cards |
| /cancelgame | Cancel the game |

## Important Notes

- The bot must be running on your computer or a server.
- Game data is stored in memory. Restarting the bot ends active games.
- Each player must privately start the bot before receiving cards.
- This is a first playable implementation, not a fully tested production release.

import json
import os
import discord
from discord import app_commands
from discord.ext import commands
import logging
import aiosqlite
import asyncio
from dotenv import load_dotenv
import webserver
import random

load_dotenv()
# TOKEN = os.getenv("DISCORD_TOKEN")
TOKEN2 = os.environ["discordkey"]
RESPONSES_PATH = "/etc/secrets/bot_responses.json"
THRESHOLD  = 0.01

if os.path.isfile(RESPONSES_PATH):
    print(f"Loading triggers from {RESPONSES_PATH}")
    with open(RESPONSES_PATH, "r", encoding="utf-8") as f:
        TRIGGERS = json.load(f)
else:
    # hehe
    TRIGGERS = {
    }

handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

# tree = app_commands.CommandTree(bot)

print("concu")

# async def init_db():
#     async with aiosqlite.connect("bets.db") as db:
#         await db.executescript(open("schema.sql").read())
#         await db.commit()

@bot.event
async def on_ready():
    await setup_database()
    synced = await bot.tree.sync()
    print(f"Synced {len(synced)} command(s).")
    print(f"Hitler's alive, {bot.user.name}")

# Database file name
DB_NAME = "bets.db"


async def setup_database():
    """
    Creates the necessary tables if they do not exist.
    """
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bets (
                bet_id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                creator_id INTEGER NOT NULL,
                is_locked INTEGER DEFAULT 0,
                outcome INTEGER DEFAULT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS options (
                option_id INTEGER PRIMARY KEY AUTOINCREMENT,
                bet_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                FOREIGN KEY (bet_id) REFERENCES bets(bet_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS wagers (
                bet_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                option_id INTEGER NOT NULL,
                stake TEXT NOT NULL,
                PRIMARY KEY (bet_id, user_id),
                FOREIGN KEY (bet_id) REFERENCES bets(bet_id),
                FOREIGN KEY (option_id) REFERENCES options(option_id)
            )
        """)
        await db.commit()
    print("Database setup complete!")


# --- UI Components ---
class StakeModal(discord.ui.Modal, title="Enter your stake"):
    """
    A modal for users to enter their stake for a given option.
    """
    stake = discord.ui.TextInput(label="Your stake (Anything)", style=discord.TextStyle.short)

    def __init__(self, bet_id: int, option_id: int):
        super().__init__()
        self.bet_id = bet_id
        self.option_id = option_id

    async def on_submit(self, interaction: discord.Interaction):
        # Defer the response immediately to handle the database query time
        await interaction.response.defer(ephemeral=True)

        async with aiosqlite.connect(DB_NAME) as db:
            # Check if bet is locked
            c = await db.execute("SELECT is_locked FROM bets WHERE bet_id = ?", (self.bet_id,))
            locked = (await c.fetchone())[0]
            if locked:
                return await interaction.followup.send("Bet is locked!", ephemeral=True)

            # Upsert (INSERT or UPDATE) the wager
            await db.execute("""
                INSERT INTO wagers (bet_id, user_id, option_id, stake)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(bet_id, user_id) DO UPDATE SET 
                    option_id=excluded.option_id, stake=excluded.stake
            """, (self.bet_id, interaction.user.id, self.option_id, self.stake.value))
            await db.commit()

        # Fetch all bettors for this bet
        async with aiosqlite.connect(DB_NAME) as db:
            c = await db.execute("SELECT user_id FROM wagers WHERE bet_id=?", (self.bet_id,))
            bettors = await c.fetchall()

        print("concu2")

        bettor_names = []
        for (user_id,) in bettors:
            member = interaction.guild.get_member(user_id)
            if member is None:
                member = await interaction.client.fetch_user(user_id)
            bettor_names.append(f"• {member.display_name}")

        bettors_text = "\n".join(bettor_names) if bettor_names else "No bets yet."

        # Edit the original bet message to show bettors
        try:
            content = interaction.message.content
            if "\n\n**Current Bettors:**" in content:
                content = content.split("\n\n**Current Bettors:**")[0]
            await interaction.message.edit(
                content=f"{content}\n\n**Current Bettors:**\n{bettors_text}",
                # view=interaction.message.components
            )
        except Exception:
            pass

        await interaction.followup.send(f"Registered: **{self.stake.value}** on option #{self.option_id}", ephemeral=True)

class BetView(discord.ui.View):
    """
    A view containing buttons for the bet message, dynamically generated
    based on the bet options.
    """
    def __init__(self, bet_id: int, creator_id: int, options: list):
        super().__init__(timeout=None)
        self.bet_id = bet_id
        self.creator_id = creator_id
        self.options = options
        
        # Dynamically create buttons for each option
        for option_id, name in self.options:
            button = discord.ui.Button(
                label=name,
                style=discord.ButtonStyle.primary,
                custom_id=f"bet_option_{option_id}"
            )
            button.callback = self.on_bet_click
            self.add_item(button)
        
        # Add the lock and refund buttons
        self.add_item(self.create_lock_button())
        self.add_item(self.create_refund_button())

    def create_lock_button(self):
        """Creates the lock bet button."""
        button = discord.ui.Button(label="Lock Bet 🔒", style=discord.ButtonStyle.danger, custom_id="lock")
        button.callback = self.lock_bet
        return button

    def create_refund_button(self):
        """Creates the refund bet button."""
        button = discord.ui.Button(label="Refund ↩️", style=discord.ButtonStyle.secondary, custom_id="refund")
        button.callback = self.refund_bet
        return button

    async def on_bet_click(self, interaction: discord.Interaction):
        """
        Callback for dynamically created bet option buttons.
        """
        if interaction.user.id != self.creator_id:
            # Check if the bet is already locked before showing the modal
            async with aiosqlite.connect(DB_NAME) as db:
                c = await db.execute("SELECT is_locked FROM bets WHERE bet_id = ?", (self.bet_id,))
                is_locked = (await c.fetchone())[0]
                if is_locked:
                    return await interaction.response.send_message("Bet is locked!", ephemeral=True)

        option_id = int(interaction.data['custom_id'].split('_')[-1])
        await interaction.response.send_modal(StakeModal(self.bet_id, option_id))

    async def lock_bet(self, interaction: discord.Interaction):
        """
        Locks the bet and disables betting buttons.
        """
        # Defer to buy time for the database update
        await interaction.response.defer()
        
        if interaction.user.id != self.creator_id:
            return await interaction.followup.send("Only the creator can lock this bet.", ephemeral=True)

        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE bets SET is_locked=1 WHERE bet_id=?", (self.bet_id,))
            await db.commit()

        # Disable all bet and refund buttons on the view
        for child in self.children:
            if child.custom_id.startswith("bet_option") or child.custom_id == "refund":
                child.disabled = True
            if child.custom_id == "lock":
                child.disabled = True
        
        # Edit the original message to reflect the changes
        await interaction.message.edit(view=self)
        await interaction.followup.send("Bet locked! Creator can now `/resolve` it.", ephemeral=True)

    async def refund_bet(self, interaction: discord.Interaction):
        """
        Allows a user to refund their bet.
        """
        # Defer to buy time for the database query
        await interaction.response.defer(ephemeral=True)

        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("DELETE FROM wagers WHERE bet_id=? AND user_id=?", 
                             (self.bet_id, interaction.user.id))
            await db.commit()

        # Fetch all bettors for this bet
        async with aiosqlite.connect(DB_NAME) as db:
            c = await db.execute("SELECT user_id FROM wagers WHERE bet_id=?", (self.bet_id,))
            bettors = await c.fetchall()

        bettor_names = []
        for (user_id,) in bettors:
            member = interaction.guild.get_member(user_id)
            if member is None:
                member = await interaction.client.fetch_user(user_id)
            bettor_names.append(f"• {member.display_name}")

        bettors_text = "\n".join(bettor_names) if bettor_names else "No bets yet."

        # Edit the original bet message to show bettors
        try:
            content = interaction.message.content
            if "\n\n**Current Bettors:**" in content:
                content = content.split("\n\n**Current Bettors:**")[0]
            await interaction.message.edit(
                content=f"{content}\n\n**Current Bettors:**\n{bettors_text}",
                # view=interaction.message.components
            )
        except Exception:
            pass

        await interaction.followup.send("Your bet was refunded.", ephemeral=True)

# --- Bot Commands ---
@bot.tree.command(name="createbet", description="Create a new friendly bet")
@app_commands.describe(title="Bet title / question", options="Comma-separated outcomes, e.g. Yes,No")
async def createbet(interaction: discord.Interaction, title: str, options: str):
    """
    Slash command to create a new bet.
    """
    # Defer the response immediately to handle the database insertion time
    await interaction.response.defer()

    try:
        opts = [o.strip() for o in options.split(",") if o.strip()]
        if len(opts) < 2:
            return await interaction.followup.send("Please provide at least two outcomes.", ephemeral=True)

        async with aiosqlite.connect(DB_NAME) as db:
            c = await db.execute("INSERT INTO bets (title, creator_id) VALUES (?,?)",
                                 (title, interaction.user.id))
            bet_id = c.lastrowid
            
            # Insert each option and collect the option_ids
            inserted_options = []
            for name in opts:
                c2 = await db.execute("INSERT INTO options (bet_id, name) VALUES (?,?)", (bet_id, name))
                inserted_options.append((c2.lastrowid, name))

            await db.commit()

        # Build description and view
        desc = "\n".join(f"• **{i}.** {name}" for i, (option_id, name) in enumerate(inserted_options, 1))
        view = BetView(bet_id, interaction.user.id, inserted_options)

        await interaction.followup.send(
            f"🎲 **Bet #{bet_id}**: {title}\n{desc}\n\nPlace your bets!",
            view=view
        )
    except Exception as e:
        await interaction.followup.send(f"An error occurred: `{e}`", ephemeral=True)


@bot.tree.command(name="resolve", description="Resolve an existing bet")
@app_commands.describe(bet_id="ID of the bet", winner_index="Index of the winning option (1,2,...)")
async def resolve(interaction: discord.Interaction, bet_id: int, winner_index: int):
    """
    Slash command to resolve a bet and announce the winners/losers.
    """
    # Defer the response immediately to handle all the database queries
    await interaction.response.defer()

    try:
        # Verify creator and bet status
        async with aiosqlite.connect(DB_NAME) as db:
            c = await db.execute("SELECT creator_id, is_locked FROM bets WHERE bet_id=?", (bet_id,))
            row = await c.fetchone()
            if not row:
                return await interaction.followup.send("No such bet.", ephemeral=True)
            
            creator_id, locked = row
            if interaction.user.id != creator_id:
                return await interaction.followup.send("Only the creator can resolve this bet.", ephemeral=True)
            if not locked:
                return await interaction.followup.send("Please lock the bet first using the button on the bet message.", ephemeral=True)

            # Find winning option_id and name
            c = await db.execute("SELECT option_id, name FROM options WHERE bet_id=?", (bet_id,))
            options_data = await c.fetchall()
            
            if not (1 <= winner_index <= len(options_data)):
                return await interaction.followup.send("Invalid option index.", ephemeral=True)
            
            winning_option_id, winning_name = options_data[winner_index - 1]

            # Mark outcome in the database
            await db.execute("UPDATE bets SET outcome=? WHERE bet_id=?", (winning_option_id, bet_id))
            
            # Fetch all wagers for the bet
            c = await db.execute("SELECT user_id, stake, option_id FROM wagers WHERE bet_id=?", (bet_id,))
            wagers = await c.fetchall()
            await db.commit()

        # Check if there were any wagers
        if not wagers:
            return await interaction.followup.send(
                f"**Results for Bet #{bet_id} is: {winning_name}! But...**\nEveryone was too scared to bet anything."
            )

        # Announce results
        lines = []
        for user_id, stake, option_id in wagers:
            member = interaction.guild.get_member(user_id)
            if member is None:
                # If member is not in the guild's cache, fetch it.
                # This is a slow operation, so deferring is crucial here.
                member = await interaction.client.fetch_user(user_id)
            
            if option_id == winning_option_id:
                lines.append(f"🏆 **{member.display_name}** won **{stake}**!")
            else:
                lines.append(f"❌ **{member.display_name}** lost **{stake}**.")

        response_text = f"**Results for Bet #{bet_id}, is: {winning_name}! Which means...**\n" + "\n".join(lines)
        await interaction.followup.send(response_text)
    
    except Exception as e:
        await interaction.followup.send(f"An error occurred: `{e}`", ephemeral=True)

@bot.event
async def on_member_join(member):
    if "lalalaa" in member.name.lower():
        msg1 = f"Welcome, {member.name}! We hope you bring lots of cat energy 🐱."
        msg2 = f"{member.mention} has raided the holy ground!! Let's give them our dearest welcomes! 🎉"
    elif "flint" in member.name.lower():
        msg1 = f"Welcome, {member.name}! May your presence light up our server like a flint spark! 🔥"
        msg2 = f"This can't be real... The leakest of all time, Lord of the Fallen, the one and only {member.mention} has come to lifesteal us once again! Give them our dearest welcomes!! 🎉"
    # elif member.name.lower().startswith("admin"):
    #     msg = f"All hail {member.name}, our new admin overlord!"
    else:
        msg1 = f"Welcome to the unholy ground, {member.name}! If you have any questions, feel free to ask."
        msg2 = f"{member.mention} just starts gooning! Let's give them our dearest welcomes! 🎉"
    await member.send(msg1)

    # Send welcome message in a specific channel (replace 'welcome' with your channel name)
    channel = discord.utils.get(member.guild.text_channels, name="announcements")
    if channel:
        await channel.send(msg2)

@bot.command()
async def say(ctx, *, message: str):
    """Anonymous repeater"""
    try:
        await ctx.message.delete()  # Delete the user's command message
    except Exception:
        pass
    await ctx.send(message)

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    key = message.content.strip().lower()
    if key in TRIGGERS:
        entries = TRIGGERS[key]
        choice = random.choices(
            population=entries,
            weights=[e["weight"] for e in entries],
            k=1
        )[0]

        total_weight = sum(e["weight"] for e in entries)
        prob = choice["weight"] / total_weight

        if prob < THRESHOLD:
            reply = "**Rare response triggered!!**\n\n" + choice["text"]
        else:
            reply = choice["text"]

        await message.channel.send(reply)
        return

    await bot.process_commands(message)

# @bot.event
# async def on_message(message):
#     if message.author == bot.user:
#         return

#     if "ok" in message.content.lower():
#         await message.channel.send(f"{message.author.mention} has initiated the VAR protocol.")
    
#     if "douma" in message.content.lower():
#         await message.channel.send(f"{message.author.name} is defending some dung.")

#     await bot.process_commands(message)

@bot.command()
async def hello(ctx):
    await ctx.send(f"Hello {ctx.author.mention}, welcome to our bed!")

# @bot.command()
# async def poll(ctx, *, question):
#     if not question:
#         await ctx.send("Please provide a question for the poll.")
#         return

#     embed = discord.Embed(title="Poll", description=question, color=discord.Color.blue())
#     message = await ctx.send(embed=embed)
#     await message.add_reaction("👍")
#     await message.add_reaction("👎")


webserver.keep_alive()
bot.run(TOKEN2, log_handler=handler, log_level=logging.DEBUG)


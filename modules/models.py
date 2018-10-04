import datetime
import discord
from peewee import *
from playhouse.postgres_ext import *
import modules.exceptions as exceptions
from modules import utilities
import logging

logger = logging.getLogger('polybot.' + __name__)

db = PostgresqlDatabase('polytopia', user='cbsteven')


class BaseModel(Model):
    class Meta:
        database = db


class Team(BaseModel):
    name = TextField(unique=False, null=False)       # can't store in case insensitive way, need to use ILIKE operator
    elo = SmallIntegerField(default=1000)
    emoji = TextField(null=False, default='')       # Changed default from nullable/None
    image_url = TextField(null=True)
    guild_id = BitField(unique=False, null=False)   # Included for possible future expanson

    class Meta:
        indexes = ((('name', 'guild_id'), True),)   # Trailing comma is required
        # http://docs.peewee-orm.com/en/3.6.0/peewee/models.html#multi-column-indexes

    def get_by_name(team_name: str, guild_id: int):
        teams = Team.select().where((Team.name.contains(team_name)) & (Team.guild_id == guild_id))
        return teams

    def completed_game_count(self):

        num_games = SquadGame.select().join(Game).join_from(SquadGame, Team).where(
            (SquadGame.team == self) & (SquadGame.game.is_completed == 'TRUE')
        ).count()
        print(f'team: {self.id} completed-game-count: {num_games}')

        return num_games

    def change_elo_after_game(self, opponent_elo, is_winner):

        if self.completed_game_count() < 11:
            max_elo_delta = 50
        else:
            max_elo_delta = 32

        chance_of_winning = round(1 / (1 + (10 ** ((opponent_elo - self.elo) / 400.0))), 3)

        if is_winner is True:
            new_elo = round(self.elo + (max_elo_delta * (1 - chance_of_winning)), 0)
        else:
            new_elo = round(self.elo + (max_elo_delta * (0 - chance_of_winning)), 0)

        elo_delta = int(new_elo - self.elo)
        print('Team chance of winning: {} opponent elo {} current ELO {}, new elo {}, elo_delta {}'.format(chance_of_winning, opponent_elo, self.elo, new_elo, elo_delta))

        self.elo = int(self.elo + elo_delta)
        self.save()

        return elo_delta


class DiscordMember(BaseModel):
    discord_id = BitField(unique=True, null=False)
    name = TextField(unique=False)
    elo = SmallIntegerField(default=1000)
    polytopia_id = TextField(null=True)
    polytopia_name = TextField(null=True)


class Player(BaseModel):
    discord_member = ForeignKeyField(DiscordMember, unique=False, null=False, backref='guildmember', on_delete='CASCADE')
    guild_id = BitField(unique=False, null=False)
    nick = TextField(unique=False, null=True)
    name = TextField(unique=False, null=True)
    team = ForeignKeyField(Team, null=True, backref='player')
    elo = SmallIntegerField(default=1000)
    trophies = ArrayField(CharField, null=True)
    # Add discord name here too so searches can hit just one table?

    def generate_display_name(self=None, player_name=None, player_nick=None):
        if player_nick:
            if player_name in player_nick:
                display_name = player_nick
            else:
                display_name = f'{player_name} ({player_nick})'
        else:
            display_name = player_name

        if self:
            self.name = display_name
            self.save()
        return display_name

    def upsert(discord_member_obj, guild_id, team=None):
        # Uses insert() with conflict_target updating (ie upsert). issue is that it returns row #, not created record
        discord_member, _ = DiscordMember.get_or_create(discord_id=discord_member_obj.id, defaults={'name': discord_member_obj.name})

        display_name = Player.generate_display_name(player_name=discord_member_obj.name, player_nick=discord_member_obj.nick)
        # http://docs.peewee-orm.com/en/latest/peewee/querying.html#upsert
        player = Player.insert(discord_member=discord_member, guild_id=guild_id, nick=discord_member_obj.nick, name=display_name, team=team).on_conflict(
            conflict_target=[Player.discord_member, Player.guild_id],  # update if exists
            preserve=[Player.team, Player.nick, Player.name]  # refresh team/nick with new value
        ).execute()

        return player

    def get_teams_of_players(guild_id, list_of_players):
        # TODO: make function async? Tried but got invalid syntax complaint in linter in the calling function

        # given [List, Of, discord.Member, Objects] - return a, b
        # a = binary flag if all members are on the same Poly team. b = [list] of the Team objects from table the players are on
        # input: [Nelluk, Frodakcin]
        # output: True, [<Ronin>, <Ronin>]

        with db:
            query = Team.select(Team.name).where(Team.guild_id == guild_id)
            list_of_teams = [team.name for team in query]               # ['The Ronin', 'The Jets', ...]
            list_of_matching_teams = []
            for player in list_of_players:
                matching_roles = utilities.get_matching_roles(player, list_of_teams)
                if len(matching_roles) == 1:
                    # TODO: This would be more efficient to do as one query and then looping over the list of teams one time for each player
                    name = next(iter(matching_roles))
                    list_of_matching_teams.append(
                        Team.select().where(
                            (Team.name == name) & (Team.guild_id == guild_id)
                        ).get()
                    )
                else:
                    list_of_matching_teams.append(None)
                    # Would be here if no player Roles match any known teams, -or- if they have more than one match

            same_team_flag = True if all(x == list_of_matching_teams[0] for x in list_of_matching_teams) else False
            return same_team_flag, list_of_matching_teams

    def get_by_string(player_string: str, guild_id: int):
        # Returns QuerySet containing players in current guild matching string. Searches against discord mention ID first, then exact discord name match,
        # then falls back to substring match on name/nick, then a lastly a substring match of polytopia ID or polytopia in-game name

        try:
            p_id = int(player_string.strip('<>!@'))
            # lookup either on <@####> mention string or raw ID #
            return Player.select(Player, DiscordMember).join(DiscordMember).where(
                (DiscordMember.discord_id == p_id) & (Player.guild_id == guild_id)
            )
        except ValueError:
            if len(player_string.split('#', 1)[0]) > 2:
                discord_str = player_string.split('#', 1)[0]
                # If query is something like 'Nelluk#7034', use just the 'Nelluk' to match against discord_name.
                # This happens if user does an @Mention then removes the @ character
            else:
                discord_str = player_str

            name_exact_match = Player.select(Player, DiscordMember).join(DiscordMember).where(
                (DiscordMember.name == discord_str) & (Player.guild_id == guild_id)
            )
            if len(name_exact_match) == 1:
                # String matches DiscordUser.name exactly
                return name_exact_match

            # If no exact match, return any substring matches
            name_substring_match = Player.select(Player, DiscordMember).join(DiscordMember).where(
                ((Player.nick.contains(player_string)) | (DiscordMember.name.contains(discord_str))) & (Player.guild_id == guild_id)
            )

            if len(name_substring_match) > 0:
                return name_substring_match

            # If no substring name matches, return anything with matching polytopia name or code
            poly_fields_match = Player.select(Player, DiscordMember).join(DiscordMember).where(
                ((DiscordMember.polytopia_id.contains(player_string)) | (DiscordMember.polytopia_name.contains(player_string))) & (Player.guild_id == guild_id)
            )
            return poly_fields_match

    def completed_game_count(self):

        num_games = SquadMemberGame.select().join(SquadGame).join(Game).join_from(SquadMemberGame, SquadMember).join(Player).where(
            (SquadMemberGame.member.player == self) & (SquadMemberGame.squadgame.game.is_completed == 'TRUE')
        ).count()

        return num_games

    class Meta:
        indexes = ((('discord_member', 'guild_id'), True),)   # Trailing comma is required


class Tribe(BaseModel):
    name = TextField(unique=True, null=False)


class TribeFlair(BaseModel):
    tribe = ForeignKeyField(Tribe, unique=False, null=False)
    emoji = TextField(null=False, default='')
    guild_id = BitField(unique=False, null=False)

    class Meta:
        indexes = ((('tribe', 'guild_id'), True),)   # Trailing comma is required
        # http://docs.peewee-orm.com/en/3.6.0/peewee/models.html#multi-column-indexes


class Game(BaseModel):
    name = TextField(null=True)
    # winner_delta = IntegerField(default=0)    # probably makes more sense for this to be on SquadGame
    # loser_delta = IntegerField(default=0)
    is_completed = BooleanField(default=False)
    is_confirmed = BooleanField(default=False)  # Use to confirm losses and filter searches?
    announcement_message = BitField(default=None, null=True)
    announcement_channel = BitField(default=None, null=True)
    date = DateField(default=datetime.datetime.today)
    completed_ts = DateTimeField(null=True, default=None)
    name = TextField(null=True)

    def details(self):
        {
            'The Ronin': {
                'lineup': [('player', 'elo_change_from_this_game', ':tribe_emoji:'), ('player', 'elo_change_from_this_game', ':tribe_emoji:')],
                'team_elo_change': 100,
                'team_obj': 'roninobj'
            },
            'The Sparkies': {
                'lineup': [('player', 'elo_change_from_this_game', ':tribe_emoji:'), ('player', 'elo_change_from_this_game', ':tribe_emoji:')],
                'team_elo_change': -100,
                'team_obj': 'sparkiesobj'
            }
        }
        # include squad?!

    def embed(self, ctx):
        if len(self.squads) != 2:
            raise exceptions.CheckFailedError('Support for games with >2 sides not yet implemented')

        home_side = self.squads[0]
        away_side = self.squads[1]
        # side_home_roster = home_side.roster()
        # side_away_roster = away_side.roster()

        winner = self.get_winner()

        game_headline = self.get_headline()
        game_headline = game_headline.replace('\u00a0', '\n')   # Put game.name onto its own line if its there

        embed = discord.Embed(title=game_headline)

        if self.is_completed == 1:
            embed.title += f'\n\nWINNER: {winner.name}'

        # Set embed image (profile picture or team logo)
            if self.team_size() == 1:
                winning_discord_member = ctx.guild.get_member(winner.discord_member.discord_id)
                if winning_discord_member is not None:
                    embed.set_thumbnail(url=winning_discord_member.avatar_url_as(size=512))
            elif winner.image_url:
                embed.set_thumbnail(url=game.winner.image_url)

        # TEAM/SQUAD ELOs and ELO DELTAS
        if home_side.team.name == 'Home' and away_side.team.name == 'Away':
            # Hide team ELO if its just generic Home/Away
            home_team_elo_str = away_team_elo_str = ''
        else:
            home_team_elo_str, home_squad_elo_str = home_side.elo_strings()
            away_team_elo_str, away_squad_elo_str = home_side.elo_strings()

        if self.team_size() == 1:
            # Hide squad ELO stats for 1v1 games
            home_squad_elo_str = away_squad_elo_str = '\u200b'

        game_data = [(home_side, home_team_elo_str, home_squad_elo_str, home_side.roster()), (away_side, away_team_elo_str, away_squad_elo_str, away_side.roster())]

        for side, elo_str, squad_str, roster in game_data:
            if self.team_size() > 1:
                embed.add_field(name=f'Lineup for Team **{side.team.name}**{elo_str}', value=squad_str, inline=False)

            for player, player_elo_str, tribe_emoji in roster:
                embed.add_field(name=f'**{player.name}** {tribe_emoji}', value=f'ELO: {player_elo_str}', inline=True)

        embed.set_footer(text=f'Status: {"Completed" if self.is_completed else "Incomplete"}  -  Creation Date {str(self.date)}')

        return embed

    def get_headline(self):
        if len(self.squads) != 2:
            raise exceptions.CheckFailedError('Support for games with >2 sides not yet implemented')

        home_name, away_name = self.squads[0].name(), self.squads[1].name()
        home_emoji = self.squads[0].team.emoji if self.squads[0].team.emoji else ''
        away_emoji = self.squads[1].team.emoji if self.squads[1].team.emoji else ''
        game_name = f'\u00a0*{self.name}*' if self.name.strip() else ''  # \u00a0 is used as an invisible delimeter so game_name can be split out easily

        return f'Game {self.id}   {home_emoji} **{home_name}** *vs* **{away_name}** {away_emoji}{game_name}'

    def team_size(self):
        return len(self.squads[0].membergame)

    def declare_winner(self, winning_side, confirm: bool):

        if len(self.squads) != 2:
            raise exceptions.CheckFailedError('Support for games with >2 sides not yet implemented')

        for squadgame in self.squads:
            if squadgame != winning_side:
                losing_side = squadgame

        # STEP 1: INDIVIDUAL/PLAYER ELO
        winning_side_ave_elo = winning_side.get_member_average_elo()
        losing_side_ave_elo = losing_side.get_member_average_elo()

        for winning_member in winning_side.membergame:
            winning_member.change_elo_after_game(my_side_elo=winning_side_ave_elo, opponent_elo=losing_side_ave_elo, is_winner=True)

        for losing_member in losing_side.membergame:
            losing_member.change_elo_after_game(my_side_elo=losing_side_ave_elo, opponent_elo=winning_side_ave_elo, is_winner=False)

        # STEP 2: SQUAD ELO
        winning_squad_elo, losing_squad_elo = winning_side.squad.elo, losing_side.squad.elo
        winning_side.elo_change_squad = winning_side.squad.change_elo_after_game(opponent_elo=losing_squad_elo, is_winner=True)
        losing_side.elo_change_squad = losing_side.squad.change_elo_after_game(opponent_elo=winning_squad_elo, is_winner=False)

        if self.team_size() > 1:
            # STEP 3: TEAM ELO
            winning_team_elo, losing_team_elo = winning_side.team.elo, losing_side.team.elo
            winning_side.elo_change_team = winning_side.team.change_elo_after_game(opponent_elo=losing_team_elo, is_winner=True)
            losing_side.elo_change_team = losing_side.team.change_elo_after_game(opponent_elo=winning_team_elo, is_winner=False)

        winning_side.is_winner = True
        winning_side.save()
        losing_side.save()

        if confirm:
            self.is_confirmed = True

        self.is_completed = True
        self.completed_ts = datetime.datetime.now()
        self.save()

    def create_game(teams, guild_id, name=None, require_teams=False):

        # Determine what Team guild members are associated with
        home_team_flag, list_of_home_teams = Player.get_teams_of_players(guild_id=guild_id, list_of_players=teams[0])  # get list of what server team each player is on, eg Ronin, Jets.
        away_team_flag, list_of_away_teams = Player.get_teams_of_players(guild_id=guild_id, list_of_players=teams[1])

        if (None in list_of_away_teams) or (None in list_of_home_teams):
            if require_teams is True:
                raise exceptions.CheckFailedError('One or more players listed cannot be matched to a Team (based on Discord Roles). Make sure player has exactly one matching Team role.')
            else:
                # Set this to a home/away game if at least one player has no matching role, AND require_teams == false
                home_team_flag = away_team_flag = False

        if home_team_flag and away_team_flag:
            # If all players on both sides are playing with only members of their own Team (server team), those Teams are impacted by the game...
            home_side_team = list_of_home_teams[0]
            away_side_team = list_of_away_teams[0]

            if home_side_team == away_side_team:
                with db:
                    # If Team Foo is playing against another squad from Team Foo, reset them to 'Home' and 'Away'
                    home_side_team, _ = Team.get_or_create(name='Home', guild_id=guild_id, defaults={'emoji': ':stadium:'})
                    away_side_team, _ = Team.get_or_create(name='Away', guild_id=guild_id, defaults={'emoji': ':airplane:'})

        else:
            # Otherwise the players are "intermingling" and the game just influences two hidden teams in the database called 'Home' and 'Away'
            with db:
                home_side_team, _ = Team.get_or_create(name='Home', guild_id=guild_id, defaults={'emoji': ':stadium:'})
                away_side_team, _ = Team.get_or_create(name='Away', guild_id=guild_id, defaults={'emoji': ':airplane:'})

        with db:
            newgame = Game.create(name=name)

            side_home_players = []
            side_away_players = []
            # Create/update Player records
            for player_discord, player_team in zip(teams[0], list_of_home_teams):
                side_home_players.append(Player.upsert(player_discord, guild_id=guild_id, team=player_team))

            for player_discord, player_team in zip(teams[1], list_of_away_teams):
                side_away_players.append(Player.upsert(player_discord, guild_id=guild_id, team=player_team))

            # Create/update Squad records
            home_squad = Squad.upsert(player_list=side_home_players)
            away_squad = Squad.upsert(player_list=side_away_players)

            home_squadgame = SquadGame.create(game=newgame, squad=home_squad, team=home_side_team)

            for squadmember in home_squad.squadmembers:
                SquadMemberGame.create(member=squadmember, squadgame=home_squadgame)

            away_squadgame = SquadGame.create(game=newgame, squad=away_squad, team=away_side_team)

            for squadmember in away_squad.squadmembers:
                SquadMemberGame.create(member=squadmember, squadgame=away_squadgame)

        return newgame, home_squadgame, away_squadgame

    def load_full_game(game_id: int):
        # Returns a single Game object with all related tables pre-fetched. or None

        game = Game.select().where(Game.id == game_id)
        subq = SquadGame.select(SquadGame, Team).join(Team, JOIN.LEFT_OUTER)

        subq2 = SquadMemberGame.select(
            SquadMemberGame, Tribe, TribeFlair, SquadMember, Squad, Player, DiscordMember, Team).join(
            SquadMember).join(
            Squad).join_from(
            SquadMemberGame, TribeFlair, JOIN.LEFT_OUTER).join(  # Need LEFT_OUTER_JOIN - default inner join would only return records that have a Tribe chosen
            Tribe, JOIN.LEFT_OUTER).join_from(
            SquadMember, Player).join(
            Team, JOIN.LEFT_OUTER).join_from(Player, DiscordMember)

        res = prefetch(game, subq, subq2)

        if len(res) == 0:
            return None
        return res[0]

    def return_participant(self, ctx, player=None, team=None):
        # Given a string representing a player or a team (team name, player name/nick/ID)
        # Return a tuple of the participant and their squadgame, ie Player, SquadGame or Team, Squadgame

        if player:
            player_obj = Player.get_by_string(player_string=player, guild_id=ctx.guild.id)
            if not player_obj:
                raise exceptions.CheckFailedError(f'Cannot find a player with name "{player}". Try specifying with an @Mention.')
            if len(player_obj) > 1:
                raise exceptions.CheckFailedError(f'More than one player match found for "{player}". Be more specific.')
            player_obj = player_obj[0]

            for squadgame in self.squads:
                for smg in squadgame.membergame:
                    if smg.member.player == player_obj:
                        return player_obj, squadgame
            raise exceptions.CheckFailedError(f'{player_obj.name} did not play in game {self.id}.')

        elif team:
            team_obj = Team.get_by_name(team_name=team, guild_id=ctx.guild.id)
            if not team_obj:
                raise exceptions.CheckFailedError(f'Cannot find a team with name "{team}".')
            if len(team_obj) > 1:
                raise exceptions.CheckFailedError(f'More than one team match found for "{team}". Be more specific.')
            team_obj = team_obj[0]

            for squadgame in self.squads:
                if squadgame.team == team_obj:
                    return team_obj, squadgame

            raise exceptions.CheckFailedError(f'{team_obj.name} did not play in game {self.id}.')
        else:
            raise exceptions.CheckFailedError('Player name or team name must be supplied for this function')

    def get_winner(self):
        # Returns player name of winner if its a 1v1, or team-name of winning side if its a group game

        for squadgame in self.squads:
            if squadgame.is_winner is True:
                if len(squadgame.membergame) > 1:
                    return squadgame.team
                else:
                    return squadgame.membergame[0].member.player

        return None


class Squad(BaseModel):
    elo = SmallIntegerField(default=1000)

    def get_matching_squad(player_list):
        # Takes [List, of, Player, Records] (not names)
        # Returns squad with exactly the same participating players. See https://stackoverflow.com/q/52010522/1281743
        query = Squad.select().join(SquadMember).group_by(Squad.id).having(
            (fn.SUM(SquadMember.player.in_(player_list).cast('integer')) == len(player_list)) & (fn.SUM(SquadMember.player.not_in(player_list).cast('integer')) == 0)
        )

        return query

    def upsert(player_list):
        # TODO: could re-write to be a legit upsert as in Player.upsert
        squads = Squad.get_matching_squad(player_list)

        if len(squads) == 0:
            # Insert new squad based on this combination of players
            sq = Squad.create()
            for p in player_list:
                SquadMember.create(player=p, squad=sq)
            return sq

        return squads[0]

    def completed_game_count(self):

        num_games = SquadGame.select().join(Game).join_from(SquadGame, Squad).where(
            (SquadGame.squad == self) & (SquadGame.game.is_completed == 'TRUE')
        ).count()
        print(f'squad: {self.id} completed-game-count: {num_games}')

        return num_games

    def change_elo_after_game(self, opponent_elo, is_winner):

        if self.completed_game_count() < 6:
            max_elo_delta = 50
        else:
            max_elo_delta = 32

        chance_of_winning = round(1 / (1 + (10 ** ((opponent_elo - self.elo) / 400.0))), 3)

        if is_winner is True:
            new_elo = round(self.elo + (max_elo_delta * (1 - chance_of_winning)), 0)
        else:
            new_elo = round(self.elo + (max_elo_delta * (0 - chance_of_winning)), 0)

        elo_delta = int(new_elo - self.elo)
        print('Squad chance of winning: {} opponent elo:{} current ELO {}, new elo {}, elo_delta {}'.format(chance_of_winning, opponent_elo, self.elo, new_elo, elo_delta))

        self.elo = int(self.elo + elo_delta)
        self.save()

        return elo_delta


class SquadMember(BaseModel):
    player = ForeignKeyField(Player, null=False, on_delete='CASCADE')
    squad = ForeignKeyField(Squad, null=False, backref='squadmembers', on_delete='CASCADE')


class SquadGame(BaseModel):
    game = ForeignKeyField(Game, null=False, backref='squads', on_delete='CASCADE')
    squad = ForeignKeyField(Squad, null=False, backref='squadgame', on_delete='CASCADE')
    team = ForeignKeyField(Team, null=False, backref='squadgame')
    elo_change_squad = SmallIntegerField(default=0)
    elo_change_team = SmallIntegerField(default=0)
    is_winner = BooleanField(default=False)
    team_chan_category = BitField(default=None, null=True)
    team_chan = BitField(default=None, null=True)   # Store category/ID of team channel for more consistent renaming-deletion

    def elo_strings(self):
        # Returns a tuple of strings for team ELO and squad ELO display. ie:
        # ('1200 +30', '1300')

        team_elo_str = str(self.elo_change_team) if self.elo_change_team != 0 else ''
        if self.elo_change_team > 0:
            team_elo_str = '+' + team_elo_str

        squad_elo_str = str(self.elo_change_squad) if self.elo_change_squad != 0 else ''
        if self.elo_change_squad > 0:
            squad_elo_str = '+' + squad_elo_str

        return (f'{self.team.elo} {team_elo_str}', f'{self.squad.elo} {squad_elo_str}')

    def get_member_average_elo(self):
        elo_list = [mg.member.player.elo for mg in self.membergame]
        return round(sum(elo_list) / len(elo_list))

    def name(self):
        if len(self.membergame) == 1:
            # 1v1 game
            return self.membergame[0].member.player.name
        else:
            # Team game
            return self.team.name

    def roster(self):
        # Returns list of tuples [(player, elo string (1000 +50), :tribe_emoji:)]
        players = []

        for mg in self.membergame:
            elo_str = str(mg.elo_change_player) if mg.elo_change_player != 0 else ''
            if mg.elo_change_player > 0:
                elo_str = '+' + elo_str
            players.append(
                (mg.member.player, f'{mg.member.player.elo} {elo_str}', mg.emoji_str())
            )

        return players


class SquadMemberGame(BaseModel):
    member = ForeignKeyField(SquadMember, null=False, backref='membergame', on_delete='CASCADE')
    squadgame = ForeignKeyField(SquadGame, null=False, backref='membergame', on_delete='CASCADE')
    tribe = ForeignKeyField(TribeFlair, null=True)
    elo_change_player = SmallIntegerField(default=0)

    def change_elo_after_game(self, my_side_elo, opponent_elo, is_winner):
        # Average(Away Side Elo) is compared to Average(Home_Side_Elo) for calculation - ie all members on a side will have the same elo_delta
        # Team A: p1 900 elo, p2 1000 elo = 950 average
        # Team B: p1 1000 elo, p2 1200 elo = 1100 average
        # ELO is compared 950 vs 1100 and all players treated equally

        num_games = self.member.player.completed_game_count()

        if num_games < 6:
            max_elo_delta = 75
        elif num_games < 11:
            max_elo_delta = 50
        else:
            max_elo_delta = 32

        chance_of_winning = round(1 / (1 + (10 ** ((opponent_elo - my_side_elo) / 400.0))), 3)

        if is_winner is True:
            new_elo = round(my_side_elo + (max_elo_delta * (1 - chance_of_winning)), 0)
        else:
            new_elo = round(my_side_elo + (max_elo_delta * (0 - chance_of_winning)), 0)

        elo_delta = int(new_elo - my_side_elo)
        print(f'Player chance of winning: {chance_of_winning} opponent elo:{opponent_elo} my_side_elo: {my_side_elo},'
                f'elo_delta {elo_delta}, current_player_elo {self.member.player.elo}, new_player_elo {int(self.member.player.elo + elo_delta)}')

        self.member.player.elo = int(self.member.player.elo + elo_delta)
        self.elo_change_player = elo_delta
        self.member.player.save()
        self.save()

        return elo_delta

    def emoji_str(self):

        if self.tribe.emoji:
            return self.tribe.emoji
        else:
            return ''


with db:
    db.create_tables([Team, DiscordMember, Game, Player, Tribe, Squad, SquadGame, SquadMember, SquadMemberGame, TribeFlair])
    # Only creates missing tables so should be safe to run each time

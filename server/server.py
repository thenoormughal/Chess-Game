import socket
import threading
import logging
import json
import uuid
from server.lobby import GameLobby
from server.game_session import GameSession
from common.message import Message
from common.constants import (
    MSG_MOVE, MSG_CHAT, MSG_CREATE_GAME, MSG_JOIN_GAME, MSG_SPECTATE, MSG_LEAVE,
    MSG_UPDATE, MSG_ERROR, MSG_GAME_STARTED, MSG_GAME_OVER, MSG_GET_GAMES, MSG_LOBBY_UPDATE
)
from server.utils import send_data, receive_data, generate_unique_id

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class ChessServer:
    def __init__(self, host='127.0.0.1', port=5555):
        self.host = host
        self.port = port
        self.server_socket = None
        self.clients = {}
        self.client_lock = threading.Lock()
        self.lobby = GameLobby()
        self.game_sessions = {}
        self.running = False

    def start(self):
        try:
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_socket.bind((self.host, self.port))
            self.server_socket.listen(10)
            self.running = True
            logger.info(f"Server started on {self.host}:{self.port}")

            while self.running:
                try:
                    client_socket, address = self.server_socket.accept()
                    client_id = generate_unique_id()
                    logger.info(f"New connection from {address}. Assigned ID: {client_id}")
                    client_thread = threading.Thread(
                        target=self.handle_client,
                        args=(client_socket, client_id)
                    )
                    client_thread.daemon = True
                    client_thread.start()
                except Exception as e:
                    if self.running:
                        logger.error(f"Error accepting client connection: {e}")
        except Exception as e:
            logger.error(f"Error starting server: {e}")
        finally:
            self.stop()

    def stop(self):
        self.running = False
        with self.client_lock:
            for client_id, client_info in list(self.clients.items()):
                try:
                    client_info["socket"].close()
                except:
                    pass
            self.clients.clear()
        if self.server_socket:
            try:
                self.server_socket.close()
            except:
                pass
        logger.info("Server stopped")

    def handle_client(self, client_socket, client_id):
        username = None
        try:
            with self.client_lock:
                self.clients[client_id] = {"socket": client_socket, "username": None}
            welcome_msg = Message("WELCOME", {"client_id": client_id})
            self.send_message(client_socket, welcome_msg)

            while self.running:
                data = receive_data(client_socket)
                if not data:
                    break
                try:
                    message = Message.from_json(data)
                    logger.debug(f"Received message: {message.msg_type} from client {client_id}")
                    self.process_message(client_id, client_socket, message)
                except ValueError as e:
                    logger.error(f"Invalid message from {client_id}: {e}")
                    error_msg = Message(MSG_ERROR, {"message": "Invalid message format"})
                    self.send_message(client_socket, error_msg)
        except Exception as e:
            logger.error(f"Error handling client {client_id}: {e}")
        finally:
            self.handle_client_disconnect(client_id, username)

    def process_message(self, client_id, client_socket, message):
        msg_type = message.msg_type
        data = message.data

        if msg_type == "SET_USERNAME":
            username = data.get("username", f"Guest_{client_id[:6]}")
            with self.client_lock:
                self.clients[client_id]["username"] = username
            logger.info(f"Client {client_id} set username to {username}")
            response = Message("SET_USERNAME_ACK", {"success": True, "username": username})
            self.send_message(client_socket, response)

        elif msg_type == MSG_CREATE_GAME:
            game_id = self.lobby.create_game(client_id)
            if game_id not in self.game_sessions:
                self.game_sessions[game_id] = GameSession(game_id, player1_id=client_id)
                self.game_sessions[game_id].add_player(client_id, client_socket)
            response = Message(MSG_CREATE_GAME, {"game_id": game_id, "role": "white"})
            self.send_message(client_socket, response)
            self.broadcast_lobby_update()

        elif msg_type == MSG_JOIN_GAME:
            game_id = data.get("game_id")
            if not game_id or game_id not in self.game_sessions:
                error_msg = Message(MSG_ERROR, {"message": "Game not found"})
                self.send_message(client_socket, error_msg)
                return
                
            if self.lobby.join_game(game_id, client_id):
                self.game_sessions[game_id].add_player(client_id, client_socket)
                response = Message(MSG_JOIN_GAME, {"game_id": game_id, "role": "black"})
                self.send_message(client_socket, response)
                
                # Start the game and notify both players
                self.game_sessions[game_id].start_game()
                
                # Get usernames for both players
                white_player_id = next(pid for pid, role in self.game_sessions[game_id].player_roles.items() if role == "white")
                white_username = self.clients[white_player_id]["username"]
                black_username = self.clients[client_id]["username"]
                
                # Create a detailed game started message with all necessary info
                start_msg = Message(MSG_GAME_STARTED, {
                    "game_id": game_id,
                    "board_fen": self.game_sessions[game_id].chess_game.fen(),
                    "white_player": white_username,
                    "black_player": black_username,
                    "time_remaining": self.game_sessions[game_id].time_remaining,
                    "turn": "white",
                    "move_history": []
                })
                
                # Send the game started message to both players
                logger.info(f"Game {game_id} started: {white_username} (White) vs {black_username} (Black)")
                for pid, sock in self.game_sessions[game_id].client_sockets.items():
                    logger.debug(f"Sending game started message to player {pid}")
                    self.send_message(sock, start_msg)
                    
                # Also send an initial update to make sure the board is correctly set up
                self.game_sessions[game_id].broadcast_state()
                
                # Update the lobby for all clients
                self.broadcast_lobby_update()
            else:
                error_msg = Message(MSG_ERROR, {"message": "Cannot join game"})
                self.send_message(client_socket, error_msg)

        elif msg_type == MSG_SPECTATE:
            game_id = data.get("game_id")
            if game_id not in self.game_sessions:
                error_msg = Message(MSG_ERROR, {"message": "Game not found"})
                self.send_message(client_socket, error_msg)
                return
                
            # Add spectator to the game in the lobby
            if self.lobby.spectate_game(game_id, client_id):
                # Add the spectator to the game session
                self.game_sessions[game_id].add_spectator(client_id, client_socket)
                logger.info(f"[SERVER] Added spectator {client_id} to game {game_id}")
                
                # Send confirmation to the client
                response = Message(MSG_SPECTATE, {"game_id": game_id})
                self.send_message(client_socket, response)
                
                # Send the current game state to the new spectator
                self.game_sessions[game_id].broadcast_state()
                
                # Log the success
                username = self.clients[client_id]["username"]
                logger.info(f"Spectator {username} ({client_id}) is now watching game {game_id}")
            else:
                error_msg = Message(MSG_ERROR, {"message": "Cannot spectate game"})
                self.send_message(client_socket, error_msg)

        elif msg_type == MSG_LEAVE:
            game_id = self.lobby.get_game_id(client_id)
            if game_id and game_id in self.game_sessions:
                self.lobby.leave_game(client_id)
                self.game_sessions[game_id].remove_client(client_id)
                if not self.game_sessions[game_id].player_roles:
                    del self.game_sessions[game_id]
                else:
                    self.game_sessions[game_id].broadcast_state()
                self.broadcast_lobby_update()

        elif msg_type == MSG_MOVE:
            game_id = self.lobby.get_game_id(client_id)
            if game_id and game_id in self.game_sessions:
                move_uci = data.get("move")
                logger.info(f"Processing move {move_uci} from client {client_id} in game {game_id}")
                
                if self.game_sessions[game_id].process_move(client_id, move_uci):
                    # Move successful - the broadcast_state method in GameSession already broadcasts the update
                    
                    # Check if the game is over
                    if self.game_sessions[game_id].chess_game.is_game_over():
                        self.game_sessions[game_id].broadcast_game_over()
                        self.lobby.games[game_id] = ("finished", self.lobby.games[game_id][1], self.lobby.games[game_id][2])
                        # Keep the game session around for a bit to allow viewers to see the final state
                        # but mark it as not accepting new players
                        self.broadcast_lobby_update()
                else:
                    error_msg = Message(MSG_ERROR, {"message": "Invalid move"})
                    self.send_message(client_socket, error_msg)

        elif msg_type == MSG_CHAT:
            # First, get the game ID for this client
            game_id = self.lobby.get_game_id(client_id)
            if not game_id:
                logger.warning(f"Chat message from {self.clients[client_id]['username']} who is not in a game")
                error_msg = Message(MSG_ERROR, {"message": "You must be in a game to send chat messages"})
                self.send_message(client_socket, error_msg)
                return
                
            # Check if the game exists
            if game_id not in self.game_sessions:
                logger.warning(f"Chat message for non-existent game {game_id}")
                error_msg = Message(MSG_ERROR, {"message": "Game not found"})
                self.send_message(client_socket, error_msg)
                return
                
            # Get message content and sender info
            message_text = data.get("message")
            username = self.clients[client_id]["username"]
            
            # Log detailed information about the chat request
            logger.info(f"CHAT MESSAGE - User: {username}, ID: {client_id}, Game: {game_id}, Message: '{message_text}'")
            
            # Determine if this is a player or spectator
            is_player = client_id in self.game_sessions[game_id].player_roles
            is_spectator = client_id in self.game_sessions[game_id].spectators
            
            logger.info(f"Sender status - Player: {is_player}, Spectator: {is_spectator}")
            
            # Get the player role if this is a player
            player_role = None
            if is_player:
                player_role = self.game_sessions[game_id].player_roles[client_id]
                logger.info(f"Player role: {player_role}")
            
            # Broadcast the chat message to all game participants
            success = self.game_sessions[game_id].broadcast_chat(username, message_text, player_role)
            
            # Log the result
            if success:
                logger.info(f"Successfully broadcast chat message from {username}")
            else:
                logger.warning(f"Failed to broadcast chat message from {username}")
                error_msg = Message(MSG_ERROR, {"message": "Failed to send chat message"})
                self.send_message(client_socket, error_msg)

        elif msg_type == MSG_GET_GAMES:
            games_data = {}
            for game_id, (state, players, _) in self.lobby.games.items():
                games_data[game_id] = {
                    "status": state,
                    "players": [self.clients.get(pid, {"username": "Unknown"})["username"] for pid in players]
                }
            response = Message(MSG_LOBBY_UPDATE, {"games": games_data})
            self.send_message(client_socket, response)

    def broadcast_lobby_update(self):
        games_data = {}
        for game_id, (state, players, _) in self.lobby.games.items():
            games_data[game_id] = {
                "status": state,
                "players": [self.clients.get(pid, {"username": "Unknown"})["username"] for pid in players]
            }
        update_msg = Message(MSG_LOBBY_UPDATE, {"games": games_data})
        with self.client_lock:
            for client_id, client_info in self.clients.items():
                try:
                    self.send_message(client_info["socket"], update_msg)
                except:
                    pass

    def send_message(self, client_socket, message):
        """Send a message to a client."""
        try:
            message_json = message.to_json()
            print(f"SERVER SENDING: {message.msg_type} to client")
            success = send_data(client_socket, message_json)
            if success:
                print(f"SUCCESS sending {message.msg_type}")
            else:
                print(f"FAILED to send {message.msg_type}")
            return success
        except Exception as e:
            logger.error(f"Error sending message: {e}")
            print(f"ERROR sending message: {e}")
            return False

    def handle_client_disconnect(self, client_id, username):
        logger.info(f"Handling disconnect for client {client_id}")
        with self.client_lock:
            if client_id in self.clients:
                try:
                    self.clients[client_id]["socket"].close()
                except:
                    pass
                del self.clients[client_id]
        game_id = self.lobby.get_game_id(client_id)
        if game_id and game_id in self.game_sessions:
            self.lobby.leave_game(client_id)
            self.game_sessions[game_id].remove_client(client_id)
            if not self.game_sessions[game_id].player_roles:
                del self.game_sessions[game_id]
            else:
                self.game_sessions[game_id].broadcast_state()
        self.broadcast_lobby_update()

if __name__ == "__main__":
    server = ChessServer()
    try:
        server.start()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        server.stop()
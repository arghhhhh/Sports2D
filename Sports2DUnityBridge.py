import socket
import json
import threading
import copy # Needed for deepcopy
import logging # For better logging
import toml # For loading the .toml config file

# Import Sports2D and its DEFAULT_CONFIG
from Sports2D import Sports2D
# Correctly import DEFAULT_CONFIG from its actual location within the Sports2D package structure
from Sports2D.Sports2D import DEFAULT_CONFIG # CONFIG_HELP is not used in this script

# Setup basic logging for the bridge
logging.basicConfig(level=logging.INFO, format='[Bridge] %(levelname)s: %(message)s')

class Sports2DUnityBridge:
    def __init__(self, host='localhost', port=12345):
        self.host = host
        self.port = port
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1) # Allow address reuse
        self.clients = []
        self.lock = threading.Lock() # For thread-safe access to self.clients

    def start(self):
        try:
            self.server_socket.bind((self.host, self.port))
            self.server_socket.listen(1)
            logging.info(f"Waiting for Unity client on {self.host}:{self.port}")
        except socket.error as e:
            logging.error(f"Failed to bind or listen on socket: {e}")
            return # Exit if we can't bind/listen

        while True:
            try:
                client_socket, addr = self.server_socket.accept()
                logging.info(f"Unity client connected from {addr}")
                with self.lock:
                    self.clients.append(client_socket)
            except socket.error as e:
                logging.error(f"Socket error in accept (server socket likely closed): {e}")
                break 
            except Exception as e:
                logging.error(f"Unexpected error in accept loop: {e}")
                break

    def _convert_numpy_to_list(self, item):
        if isinstance(item, list):
            return [self._convert_numpy_to_list(i) for i in item]
        if isinstance(item, dict):
            return {k: self._convert_numpy_to_list(v) for k, v in item.items()}
        # Check for numpy array specifically if numpy is imported and used by Sports2D
        # For now, relying on tolist() method which is common for array-like objects
        if hasattr(item, 'tolist'): 
            return item.tolist()
        return item

    def send_data(self, data):
        if not self.clients:
            return

        data_to_send = self._convert_numpy_to_list(data)
        
        json_payload = ""
        try:
            json_payload = json.dumps(data_to_send) + '\n' # Add newline delimiter
            encoded_payload = json_payload.encode('utf-8')
        except TypeError as e:
            logging.error(f"JSON serialization error: {e}. Data: {data}")
            return

        with self.lock:
            for client in list(self.clients): # Iterate over a copy for safe removal
                try:
                    client.sendall(encoded_payload)
                except socket.error as e:
                    logging.warning(f"Socket error sending data: {e}. Removing client {client.getpeername()}.")
                    self.clients.remove(client)
                    try:
                        client.close()
                    except:
                        pass 
                except Exception as e:
                    logging.error(f"Unexpected error sending data: {e}")


    def close(self):
        logging.info("Closing server socket and all client connections.")
        with self.lock:
            for client in self.clients:
                try:
                    client.shutdown(socket.SHUT_RDWR) # Gracefully shutdown
                    client.close()
                except socket.error:
                    pass # Ignore errors if already closed or problematic
                except Exception as e:
                    logging.error(f"Error closing client socket: {e}")
            self.clients.clear()
        
        try:
            self.server_socket.close()
        except socket.error as e:
            logging.error(f"Error closing server socket: {e}")
        except Exception as e:
            logging.error(f"Unexpected error closing server socket: {e}")


def process_with_unity(initial_config_overrides):
    bridge = Sports2DUnityBridge()
    
    server_thread = threading.Thread(target=bridge.start)
    server_thread.daemon = True 
    server_thread.start()

    config_dict = copy.deepcopy(DEFAULT_CONFIG)

    # Apply base overrides
    if 'base' not in config_dict: config_dict['base'] = {}
    base_overrides = initial_config_overrides.get('base', {})
    for key, value in base_overrides.items():
        config_dict['base'][key] = value
    
    # Apply pose overrides (example)
    if 'pose' not in config_dict: config_dict['pose'] = {}
    pose_overrides = initial_config_overrides.get('pose', {})
    for key, value in pose_overrides.items():
        config_dict['pose'][key] = value

    # Apply post-processing overrides
    if 'post-processing' not in config_dict: config_dict['post-processing'] = {}
    post_processing_overrides = initial_config_overrides.get('post-processing', {})
    for key, value in post_processing_overrides.items():
        config_dict['post-processing'][key] = value

    # Ensure 'logging' section exists and set custom logging to False for Sports2D
    # so bridge can handle its own logging.
    if 'logging' not in config_dict: config_dict['logging'] = {}
    config_dict['logging']['use_custom_logging'] = False

    def process_callback_for_unity(frame_data):
        # logging.debug(f"Callback: Received frame data, type: {type(frame_data)}")
        bridge.send_data(frame_data)

    config_dict['process_callback_for_unity'] = process_callback_for_unity
    
    main_process_thread = None
    try:
        logging.info("Starting Sports2D.process in a new thread...")
        # Run Sports2D.process in its own thread so the bridge can be closed if Sports2D hangs
        main_process_thread = threading.Thread(target=Sports2D.process, args=(config_dict,))
        main_process_thread.daemon = True # Allow exiting if bridge closes
        main_process_thread.start()
        
        # Keep the main thread (process_with_unity) alive while the process thread runs
        # or until an external signal (like Ctrl+C) stops it.
        while main_process_thread.is_alive():
            main_process_thread.join(timeout=0.5) # Check every 0.5s

    except KeyboardInterrupt:
        logging.info("KeyboardInterrupt received. Shutting down bridge and Sports2D process.")
    except Exception as e:
        logging.error(f"Error during Sports2D.process execution: {e}")
        import traceback
        traceback.print_exc()
    finally:
        logging.info("Initiating shutdown of the bridge...")
        bridge.close()
        if main_process_thread and main_process_thread.is_alive():
            logging.info("Sports2D process thread is still alive, waiting for it to join (max 5s)...")
            main_process_thread.join(timeout=5) # Give it a moment to finish
            if main_process_thread.is_alive():
                 logging.warning("Sports2D process thread did not terminate gracefully.")
        logging.info("Sports2D Unity Bridge finished.")


if __name__ == "__main__":
    config_file_path = 'config_unity_bridge.toml'
    loaded_config_overrides = {}
    try:
        with open(config_file_path, 'r') as f:
            loaded_config_overrides = toml.load(f)
        logging.info(f"Successfully loaded bridge config from {config_file_path}")
    except FileNotFoundError:
        logging.error(f"ERROR: Bridge configuration file '{config_file_path}' not found. Using default bridge overrides.")
        # Fallback to some essential defaults if file not found, or you could make it mandatory
        loaded_config_overrides = {
            'base': {
                'video_input': 'webcam',
                'show_realtime_results': False,
                'disable_internal_data_accumulation': True,
                'person_ordering_method': 'first_detected',
                'save_pose': False,
                'save_angles': False
            },
            'post-processing': {
                'interpolate': False, 
                'filter': False,
                'show_graphs': False
            }
        }
    except toml.TomlDecodeError as e:
        logging.error(f"ERROR: Could not decode bridge configuration file '{config_file_path}': {e}. Using default bridge overrides.")
        # Fallback similarly
        loaded_config_overrides = {
            'base': {
                'video_input': 'webcam',
                'show_realtime_results': False,
                'disable_internal_data_accumulation': True,
                'person_ordering_method': 'first_detected',
                'save_pose': False,
                'save_angles': False
            },
            'post-processing': {
                'interpolate': False, 
                'filter': False,
                'show_graphs': False
            }
        }

    logging.info("Starting Sports2D with Unity Bridge...")
    process_with_unity(loaded_config_overrides)
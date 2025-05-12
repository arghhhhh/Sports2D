import socket
import json
import threading
import copy # Needed for deepcopy
import logging # For better logging

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
    our_config_overrides = {
        'base': {
            'video_input': 'webcam',        # Use 'webcam' or a video file path
            'show_realtime_results': False, # Sports2D's own GUI is not needed
            'calculate_angles': True,
            'nb_persons_to_detect': 1,      # Example: detect 1 person
            'result_dir': 'Sports2D_Results_Bridge', # Specify a results directory
            'save_vid': False,              # Don't save video from Sports2D by default
            'save_img': False,              # Don't save images from Sports2D by default
            'save_pose': True,             # Optionally save TRC files from Sports2D
            'save_angles': True,            # Optionally save MOT files from Sports2D
        },
        'pose': {
            'det_frequency': 5,             # Example: run detection less frequently
        },
        # 'logging': { # Control Sports2D's internal logging if needed, but we set use_custom_logging to False
        # 'use_custom_logging': False 
        # }
    }
    
    logging.info("Starting Sports2D with Unity Bridge...")
    process_with_unity(our_config_overrides)
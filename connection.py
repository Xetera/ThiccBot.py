import pickle
import socket


class Server:
	def __init__(self):
		self.IP = socket.AF_INET
		self.serversocket = socket.socket(
			socket.AF_INET, socket.SOCK_STREAM)
		self.host = socket.gethostname()
		self.port = 8080
		self.received = []

	def bind(self):
		self.serversocket.bind((self.host, self.port))

	def receive_request(self, sock):
		packet = pickle.loads(sock.recv(1024)).strip()
		print(packet)



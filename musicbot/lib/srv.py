import socket
import threading
import pickle
import asyncio

class ThreadedServer(object):
	def __init__(self, host, port, bot):
		self.bot = bot
		self.host = socket.gethostname()
		self.port = port
		self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
		self.sock.bind((self.host, self.port))
		print("Initializing Server")
		self.connection = None

	def listen(self):
		self.sock.listen(5)
		while True:
			client, address = self.sock.accept()
			print('Got a connection from {}'.format(client))
			threading.Thread(target=self.listenToClient, args=(client, address)).start()

	def listenToClient(self, client, address):
		size = 1024
		while True:
			try:
				data = client.recv(size)
				self.connection = client
				decode = pickle.loads(data)

				# Set the response to echo back the recieved data
				print('Got a request from Whatsapp Bot\n{}'.format(decode))

				if decode == 'discord':
					self.bot.loop.create_task(self.bot.fetch_whatsapp_info())




				else:
					del self.connection
					raise Exception('Client disconnected')
			except:
				client.close()
				return False

	async def sendDiscordInformation(self, packet):
		print('Sending WhatsappBot status information:\n'
		      '{}'.format(packet))
		formatted = 'discord\n{}'.format(packet)
		self.connection.send(pickle.dumps(formatted))


if __name__ == '__main__':

	thd = ThreadedServer('68.4.235.189', 8080)
	threading.Thread(target=thd.listen).start()
	thd.startBot()

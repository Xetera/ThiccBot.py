import requests
from bs4 import BeautifulSoup
from urllib.request import urlopen
import json
import pprint
import random

API_KEY = "dKwKU0ZoUl9iETXEURBd8bddOvAIFT01"
search = "world of warcraft"
r = requests.get("https://api.giphy.com/v1/gifs/search?api_key={}&q={}&limit=1&offset=0&rating=G&lang=en".format(API_KEY, search))
rjson = json.loads(r.text)

print(rjson)

for k, v in rjson['data'][0].items():
    if k == "images":
        for k2, v2 in v.items():
            if k2 == "downsized_medium":
                for k3, v3 in v2.items():
                    if k3 == "url":
                        print(v3)


"""
for k in rjson['data']:
    for k2, v2 in k.items():
        if k2 == "images":
            for k3, v3 in v2.items():
                if k3 == "original":
                    for k4, v4 in v3.items():
                        if k4 == "url":
                            print(v4)




#print(rjson['data'])
#pprint.pprint(rjson)

"""
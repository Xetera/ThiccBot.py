import json
import requests
import pprint
from itertools import groupby
from operator import itemgetter
from datetime import datetime
api_key = "&APPID=507e30d896f751513350c41899382d89"
city_name_url = "http://api.openweathermap.org/data/2.5/weather?q="
units = "&units=metric"

city = "london"
urlrequest = city_name_url + city + units + api_key
response = requests.get(urlrequest)
data = json.loads(response.text)

print(data)
city = '2643743'

forecast_url = "http://api.openweathermap.org/data/2.5/forecast?id="

forecast_request = forecast_url + city + units + api_key
forecast_response = requests.get(forecast_request)
forecast = json.loads(forecast_response.text)
pprint.pprint(forecast)

dates = [y['dt'] for y in forecast['list']]
date_array = []
print(dates)

for i in range(0, len(dates), 2):

for i in range(len(days)):
    date_array.append(datetime.fromtimestamp(float(days[i])).strftime('%m/%d/%Y %H'))
print(date_array)
'''
for d in forecast['list']:
    for k, v in d.items():
        if v in days:
            print(d)
            print(d['weather'][0]['description'])
            print(d['main']['temp'])
#print(datetime.fromtimestamp(forecast['list'][0]['dt']))
'''
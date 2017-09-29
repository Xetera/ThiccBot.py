# name: Ali
# date: 7/12/2016
# description: uses openweathermap.org's api to get weather data about
# the city that is inputted

# unbreakable? = idk
import json
import urllib2
from collections import OrderedDict
from pprint import pprint
api_key = "&APPID=507e30d896f751513350c41899382d89"
city_name_url = "http://api.openweathermap.org/data/2.5/weather?q="
units = "&units=metric"

general_info = {
    "Humidity (%)": 0,
    "Pressure": 0,
    "Temperature(C)": 0,
    "Max. Temp.(C)": 0,
    "Min. Temp.(C)": 0
    }

def connectapi():
    global parsed
    global data
    urlrequest = city_name_url + city_input + units + api_key
    response = urllib2.urlopen(urlrequest)
    content = response.read()

    data = json.loads(content, object_pairs_hook=OrderedDict)
    parsed = json.dumps(data, indent=4, sort_keys=True)
    print parsed


def find_data():
    global country_name
    global city_name
    global general_info
    global weather_description
    global formatted_general_info
    city_name = str(data['name'])
    country_name = str(data['sys']['country'])
    #weather_description = data['weather']['description']
    for key, value in data['main'].iteritems():
       if key == "humidity":
           general_info['Humidity (%)'] = value
       elif key == "pressure":
           general_info['Pressure'] = value
       elif key == "temp":
           general_info['Temperature(C)'] = value
       elif key == "temp_max":
           general_info['Max. Temp.(C)'] = value
       elif key == "temp_min":
           general_info['Min. Temp.(C)'] = value
       else:
           continue




print "Weather Lookup\n\nEnter the name of the city that you want\nto look at the weather details of.\n"
while True:

    try:
        city_input = str(raw_input("What city would you like to look at?"))
    except ValueError:
        print"Please enter a city name."

    connectapi()
    if "name" in data:
        find_data()
        print "\n%r in %r:\n"% (city_name, country_name)
        print """General info:"""
        pprint(general_info)
        print "\nWeather Description:\n\tidk why it doesn't let me take this data so annoying\n"
    else:
        print "Something went wrong, would you like to try again?"
        continue



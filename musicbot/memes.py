from PIL import Image
from PIL import ImageDraw
from PIL import ImageFont
import textwrap
import os
scriptDir = os.path.dirname(__file__)

class Meme:
    def __init__(self, type):
        # we'll change this as we add support for more memes
        self.bin = ['brain']

        if type in self.bin:
            self.type = type
        else:
            raise TypeError("{} is not a valid type".format(type))

    @staticmethod
    def get_response(status, action, info):
        return {"status": status, "action": action, "info": info}

    def generate_meme(self, args):
        pass


class Brain(Meme):
    def __init__(self):
        super(Brain, self).__init__("brain")
        self.picture = None
        self.box1 = {}
        self.text_size = 50

    def set_picture(self, args):
        if len(args) != 4:
            return Meme.get_response(400, "error", "request out of bound")

        self.picture = 'brain{}.jpg'.format(len(args))
        return Meme.get_response(200, "OK", "")

    @staticmethod
    def midpoint(x1, x2):
        return (x1 + x2) / 2

    def settext4(self, args):
        box_size_iter = 300

        # people like leaving spaces after commas
        cleaned = [x.strip() for x in args]

        img = Image.open(scriptDir +"\\meme_folder\\" + self.picture)
        draw = ImageDraw.Draw(img)

        font = ImageFont.truetype("./arial.ttf", self.text_size)
        margin = offset = 40

        # draw.line(((40, 40), (40, 40 + 900)), fill="red")
        texts = cleaned
        start_point = 40
        for i in texts:
            text = textwrap.wrap(i, width=12)
            offset = start_point
            for line in text:
                draw.text((margin, offset), line, font=font, fill="black")
                offset += font.getsize(line)[1]
            start_point = start_point + box_size_iter

        img.save(scriptDir + "\\meme_folder\\test.jpg")


    def generate_meme(self, args):
        """
        :param args: array
        :return:
        """
        if self.type != 'brain':
            return Meme.get_response(404, "error", "invalid type")

        return self.set_picture(args)

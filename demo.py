# 需安装第三方requests
# img_url，图片存放路径
# 读取图片，并获取图片的base64数据
import base64,requests
api_post_url = "http://www.bingtop.com/ocr/upload/"
img_url = r'C:\images\图片地址.jpg'
with open(img_url,'rb') as pic_file:
    img64=base64.b64encode(pic_file.read())
params = {
    "username": "%s" % api_username,
    "password": "%s" % api_password,
    "captchaData": img64,
    "captchaType": 2303
}
response = requests.post(api_post_url, data=params)
dictdata=json.loads(response.text)
# dictdata: {"code":0, "message":"", "data":{"captchaId":"1001-158201918112812","recognition":"RESULT"}}
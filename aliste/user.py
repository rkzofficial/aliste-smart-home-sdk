class User:
    accesstoken: str
    email: str
    name: str
    homeId: str
    mobile: str
    credentials: dict

    def __init__(self, accesstoken, email, name, homeId, mobile, credentials):
        self.accesstoken = accesstoken or ""
        self.email = email or ""
        self.name = name or ""
        self.homeId = homeId or ""
        self.mobile = mobile or ""
        self.credentials = credentials or {}

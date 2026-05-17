import httpx

from app.config import settings


async def exchange_google_code(code: str) -> dict:
    token_url = "https://oauth2.googleapis.com/token"
    data = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": "postmessage",
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(token_url, data=data)
        resp.raise_for_status()
        tokens = resp.json()

    userinfo_url = "https://www.googleapis.com/oauth2/v2/userinfo"
    async with httpx.AsyncClient() as client:
        resp = await client.get(userinfo_url, headers={"Authorization": f"Bearer {tokens['access_token']}"})
        resp.raise_for_status()
        userinfo = resp.json()

    return {
        "provider": "google",
        "oauth_id": userinfo["id"],
        "email": userinfo.get("email", ""),
        "full_name": userinfo.get("name"),
        "avatar_url": userinfo.get("picture"),
    }


async def exchange_github_code(code: str) -> dict:
    token_url = "https://github.com/login/oauth/access_token"
    data = {
        "client_id": settings.GITHUB_CLIENT_ID,
        "client_secret": settings.GITHUB_CLIENT_SECRET,
        "code": code,
        "redirect_uri": settings.GITHUB_REDIRECT_URI,
    }
    headers = {"Accept": "application/json"}
    async with httpx.AsyncClient() as client:
        resp = await client.post(token_url, data=data, headers=headers)
        resp.raise_for_status()
        tokens = resp.json()

    if "access_token" not in tokens:
        raise ValueError(f"GitHub token exchange failed: {tokens}")

    user_url = "https://api.github.com/user"
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            user_url,
            headers={
                "Authorization": f"Bearer {tokens['access_token']}",
                "Accept": "application/json",
            },
        )
        resp.raise_for_status()
        userinfo = resp.json()

    # GitHub may not expose email on the user endpoint; fetch separately if needed
    email = userinfo.get("email", "")
    if not email:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://api.github.com/user/emails",
                headers={
                    "Authorization": f"Bearer {tokens['access_token']}",
                    "Accept": "application/json",
                },
            )
            resp.raise_for_status()
            emails = resp.json()
            primary = next((e for e in emails if e.get("primary")), None)
            if primary:
                email = primary["email"]

    return {
        "provider": "github",
        "oauth_id": str(userinfo["id"]),
        "email": email,
        "full_name": userinfo.get("name") or userinfo.get("login"),
        "avatar_url": userinfo.get("avatar_url"),
    }

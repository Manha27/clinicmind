def test_ui_is_served_and_root_redirects_to_it(client):
    page = client.get("/ui/")
    assert page.status_code == 200 and "ClinicMind" in page.text and "<script>" in page.text
    redirect = client.get("/", follow_redirects=False)
    assert redirect.status_code in (302, 307) and redirect.headers["location"] == "/ui/"

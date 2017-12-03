"""
Strava OAUTH flow hacked together.

XXX: the manual strava_client  extensions hack sucks.
"""
from urllib.parse import urlunparse, urlencode, parse_qsl

import dateutil.parser
from flask import Blueprint, redirect, request, url_for, render_template, abort, current_app, flash
import flask_login

import tourmap.utils
import tourmap.utils.strava
from tourmap.database import db
from tourmap import database
from tourmap.models import User, PollState, Token, Tour

class LoginController(object):
    """
    """
    def __init__(self, session=None):
        self.__session = session or db.session

    def strava_login(self, data):
        """
        Given data from a strava token exchange, either update and return
        an existing user, or create a new one and return that.

        XXX: This is a mess!

        :param data: as returned by a token exchange from Strava.
        :returns: tuple representing (new_user, user)
        """
        athlete = data["athlete"]
        new_user = False
        tour = None
        user = User.query.filter_by(strava_id=athlete["id"]).one_or_none()
        if user is None:
            user = User(strava_id=athlete["id"])
            tour = Tour(
                user=user,
                name="All Activities",
                description="Automatically created tour."
            )
            new_user = True

        # Update this user object, should check for changes, actually
        # to not needlessly do DB commits?
        user.email = athlete.get("email")
        user.firstname = athlete.get("firstname"),
        user.lastname = athlete.get("lastname")
        user.country = athlete.get("country")

        self.__session.add_all(filter(None, [user, tour]))
        self.__session.commit()

        # Create or update the token if neede.
        token = Token.query.filter_by(user=user).one_or_none()
        if token is None:
            token = Token(user=user)

        if data["access_token"] != token.access_token:
            current_app.logger.info("Setting token for %s", user)
            token.access_token = data["access_token"]

        poll_state = PollState.query.filter_by(user=user).one_or_none()
        if poll_state is None:
            poll_state = PollState(
                user=user,
                full_fetch_next_page=0,
                full_fetch_completed=False,
            )

        # Ok, go figure it out for us...
        self.__session.add_all([token, poll_state])

        try:
            self.__session.commit()
        except database.IntegrityError as e:
            # Now, this can happen if someone tries to sign-up the
            # same account at the same time. But than he can just
            # retry...
            current_app.logger.exception("User %s: %s", user, e)
            db.session.rollback()
            abort(500)

        return new_user, user


def create_blueprint(app):
    bp = Blueprint("strava", __name__)

    # XXX: This works only with a single thread or bad stuff might happen.
    # Check Flask-Plugins and pooling...
    __strava_client = tourmap.utils.strava.StravaClient.from_env(environ=app.config)
    app.extensions["strava_client"] = __strava_client

    @bp.route("/login")
    def login():
        """
        Just the Connect with Strava template.
        """
        return render_template("strava/login.html")

    @bp.route("/logout")
    def logout():
        """
        Do a logout.
        """
        flask_login.logout_user()
        return redirect(url_for("index"))

    @bp.route("/callback")
    def callback():
        """
        XXX: Needs some serious error checking!
        XXX: This should really be a controller and not in a view...
        XXX: The strava client is not thread safe!
        """
        strava_client = current_app.extensions["strava_client"]

        if "error" in request.args:
            msg = "Connect with Strava failed: {!r}".format(request.args["error"])
            current_app.logger.warning(msg)
            flash(msg, category="error")
            return redirect(url_for("strava.login"))

        state = request.args.get("state")
        state_dict = dict(parse_qsl(state))
        if state_dict.get("state") != "CONNECT":
            msg = "Connect with Strava failed (state was {!r})".format(state)
            current_app.logger.error(msg)
            flash(msg, category="error")
            return redirect(url_for("strava.login"))

        try:
            data = strava_client.exchange_token(request.args["code"])
        except tourmap.utils.strava.StravaBadRequest as e:
            msg = "Connect with Strava failed: {!r}".format(e.errors)
            current_app.logger.error(msg)
            flash(msg, category="error")
            return redirect(url_for("strava.login"))

        new_user, user = LoginController().strava_login(data)

        # At this point we can be somewhat sure the user has a Strava
        # account and that is good enough for us to log him in.
        current_app.logger.info("%s just logged in!", user)
        flask_login.login_user(tourmap.utils.UserProxy(user))

        if new_user:
            flash("Successfully connected with Strava. Thanks!", category="success")
            flash("Fetching your activities in the background, "
                  "just refresh this page until they show up ;-)", category="info")

        # Not sure this is working properly... If we got here through a
        # redirect it should go back to the original page.
        try:
            return tourmap.utils.redirect_back(
                default_endpoint="users.user",
                next_candidate=state_dict.get("next"),
                hashid=user.hashid
            )
        except Exception as e:
            current_app.logger.exception("Redirect exception... %s", e)
            raise

    @bp.route("/authorize")
    def authorize():
        """
        Redirect the user to Strava asking to authorize our app.
        """
        app.logger.info("Strava authorize call!")

        # XXX: This may break behind a proxy, or maybe not?
        components = (request.scheme, request.host, url_for("strava.callback"), None, None, None)
        redirect_uri = urlunparse(components)
        strava_client = current_app.extensions["strava_client"]

        state = {
            "state": "CONNECT",
        }
        if request.args.get("next"):
            state["next"] = request.args.get("next")

        return redirect(strava_client.authorize_redirect_url(
            redirect_uri=redirect_uri,
            scope=None,
            state=urlencode(state),
            approval_prompt="auto"
        ))

    @bp.route("/proxy/<int:user_id>/activities")
    def activities(user_id):
        user = User.query.get_or_404(user_id)
        token = Token.query.filter_by(user_id=user.id).one_or_none()
        if token is None:
            abort(404)

        page = int(request.args.get("page")) if "page" in request.args else None

        try:
            strava_client = current_app.extensions["strava_client"]
            activities = strava_client.activities(token=token.access_token, page=page)
        except tourmap.utils.strava.StravaTimeout:
            app.logger.warning("Strava timeout...")
            abort(504)

        cleaned_activities = []
        for a in activities:
            ca = {
                "name": a["name"],
                "distance": round(a["distance"] / 1000.0, 2),
                "date": dateutil.parser.parse(a["start_date_local"]).date(),
            }
            cleaned_activities.append(ca)

        return render_template("strava/activities.html", user=user, activities=cleaned_activities)
    return bp

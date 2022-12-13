import json
import os

from contextlib import contextmanager
from rq import Queue
from rq.job import Job
from worker import conn

from flask import Flask, jsonify, request, send_from_directory, redirect, url_for
from flask.cli import FlaskGroup
from flask_sqlalchemy import SQLAlchemy
import yt_dlp


basedir = os.path.abspath(os.path.dirname(__file__))


class Config(object):
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", "sqlite:///./app.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    MEDIA_FOLDER = "/download"


app = Flask(__name__)

app.config.from_object(Config)

db = SQLAlchemy(app)

q = Queue(connection=conn)


class DownloadLogger:
    def debug(self, msg):
        # For compatibility with youtube-dl, both debug and info are passed into debug
        # You can distinguish them by the prefix '[debug] '
        if msg.startswith('[debug] '):
            pass
        else:
            self.info(msg)

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        print(msg)


def progress_hook(d):
    if d['status'] == 'finished':
        print('Done downloading, now post-processing ...')


ydl_opts = {
    'logger': DownloadLogger(),
    'progress_hooks': [progress_hook],
    'outtmpl': '/download/%(id)s.%(ext)s',
}


class Video(db.Model):
    __tablename__ = "videos"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String)
    identifier = db.Column(db.String, unique=True)
    description = db.Column(db.String)
    url = db.Column(db.String)

    def to_dict(self):
        data = {
            "id": self.id,
            "youtube_id": self.identifier,
            "title": self.title,
            "description": self.description,
            "url": self.url
        }

        return data

    def from_json(self, data):
        self.identifier = data['id']
        self.title = data['title']
        self.description = data['description']

    def __repr__(self):
        return f'<Video({self.identifier})>'


@contextmanager
def no_expire():
    s = db.session()
    s.expire_on_commit = False
    try:
        yield
    finally:
        s.expire_on_commit = True


@app.route("/")
def hello_world():
    return jsonify(hello="world")


@app.route("/media/<path:filename>")
def mediafiles(filename):
    filename = f'{filename}.mp4'
    return send_from_directory(app.config["MEDIA_FOLDER"], filename)


def save_video(url):
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

        data = ydl.sanitize_info(info)

        video = Video()

        video.from_json(data)
        with app.app_context():
            with no_expire():
                db.session.add(video)

                db.session.commit()

        ydl.download(url)

        return video.id


@app.route("/watch")
def watch():
    from app import save_video

    identifier = request.args.get('v')

    video = Video.query.filter_by(identifier=identifier).first()

    if video and video.url:
        return redirect(video.url)

    url = f'https://www.youtube.com/watch?v={identifier}'

    job = q.enqueue_call(func=save_video, args=(url,), result_ttl=5000)

    # return jsonify(identifier=identifier, job=job.get_id())
    return redirect(url_for('get_results', job_key=job.get_id()))


@app.route("/results/<job_key>", methods=['GET'])
def get_results(job_key):

    job = Job.fetch(job_key, connection=conn)

    if job.is_finished:
        video = Video.query.filter_by(id=job.result).first()
        if video.url is None:
            video.url = url_for('mediafiles', filename=video.identifier)
            db.session.commit()
        # return jsonify(video.to_dict())
        return redirect(video.url)
    else:
        return "Nay!", 202


cli = FlaskGroup(app)


@cli.command("create_db")
def create_db():
    with app.app_context():
        db.drop_all()
        db.create_all()
        db.session.commit()


def main():
    with app.app_context():
        db.create_all()

    cli()


if __name__ == '__main__':
    main()

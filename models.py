from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

class Brand(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    analyses = db.relationship('AnalysisHistory', backref='brand', lazy=True)

class AnalysisHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    brand_id = db.Column(db.Integer, db.ForeignKey('brand.id'), nullable=False)
    date = db.Column(db.DateTime, default=datetime.utcnow)
    followers = db.Column(db.Integer, default=0)
    avg_likes = db.Column(db.Float, default=0.0)
    avg_comments = db.Column(db.Float, default=0.0)
    avg_views = db.Column(db.Float, default=0.0)
    engagement_rate = db.Column(db.Float, default=0.0)

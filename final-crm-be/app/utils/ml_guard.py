import joblib
import os
import re
import logging
import sys
from typing import Tuple, Optional, Dict, Any
from app.models.ticket import TicketPriority

logger = logging.getLogger(__name__)

# ==========================================
# 1. PREPROCESSOR (EXACT COPY FROM TRAINING)
# ==========================================
class MultiServicePreprocessor:
    def __init__(self):
        self.noise_words = [
            'tolong','mohon','bantu','gan','sis','min','kak','minta','harap',
            'halo','permisi','mas','mbak','pak','bu','om','boss','bro'
        ]

        self.service_keywords = {
            'listrik': ['listrik','mcb','token','kwh','meteran','voltase','padam','mati','sekring'],
            'air': ['air','pdam','pipa','bocor','pompa','keruh','tekanan'],
            'internet': ['internet','wifi','modem','router','ont','fiber','lemot','koneksi'],
            'gas': ['gas','lpg','tabung','regulator','kompor','selang'],
            'sanitasi': ['wc','toilet','closet','septictank','saluran','mampet'],
            'ac': ['ac','air conditioner','dingin','freon','kompresor'],
            'elektronik': ['kulkas','mesin cuci','tv','kipas','lampu','setrika'],
            'gedung': ['lift','elevator','eskalator','genset','cctv'],
            'telepon': ['telepon','fax','pabx'],
            'tv_kabel': ['tv kabel','parabola','decoder','channel']
        }

        self.urgency_keywords = [
            'mati total','down','meledak','terbakar','asap','api',
            'bocor','banjir','meluap','bahaya','darurat','cepat',
            'segera','urgent','parah','berbahaya'
        ]

        self.billing_keywords = [
            'tagihan','bayar','biaya','invoice','mahal','tarif',
            'denda','telat','tertunggak','pasang baru','upgrade'
        ]

    def clean_text(self, text):
        text = str(text).lower()
        for w in self.noise_words:
            text = re.sub(rf'\b{w}\b', '', text)
        text = re.sub(r'[^a-z0-9\s]', ' ', text)
        return re.sub(r'\s+', ' ', text).strip()

    def inject_features(self, text):
        tags = []

        for svc, kws in self.service_keywords.items():
            if any(k in text for k in kws):
                tags.append(f"__SVC_{svc.upper()}__")

        if any(k in text for k in self.urgency_keywords):
            tags.extend(["__URGENT__"] * 3)

        if any(k in text for k in self.billing_keywords):
            tags.append("__BILLING__")

        return tags

    def process_batch(self, texts):
        out = []
        for t in texts:
            clean = self.clean_text(t)
            tags = self.inject_features(clean)
            out.append(f"{clean} {' '.join(tags)}".strip())
        return out

# ==========================================
# 2. ML GUARD CLASSIFIER (WRAPPER)
# ==========================================
class MLGuardClassifier:
    _instance = None
    
    # Components
    category_model = None
    priority_model = None
    preprocessor = None
    vectorizer = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(MLGuardClassifier, cls).__new__(cls)
            cls._instance._load_model()
        return cls._instance

    def _load_model(self):
        """
        Load the dictionary-based .pkl package
        """
        try:
            model_path = os.getenv("NLP_MODEL_PATH", "app/ml_models/multi_service_models_v6.pkl")
            
            if os.path.exists(model_path):
                logger.info(f"🔄 Loading NLP Guard Model from {model_path}...")
                
                # [CRITICAL FIX] Map the class to __main__ so pickle can find it
                import __main__
                setattr(__main__, "MultiServicePreprocessor", MultiServicePreprocessor)
                
                package = joblib.load(model_path)
                
                self.category_model = package.get('category_model')
                self.priority_model = package.get('priority_model')
                self.preprocessor = package.get('preprocessor')
                
                # Extract Vectorizer safely
                base_estimator = self.category_model
                if hasattr(self.category_model, 'calibrated_classifiers_'):
                    base_estimator = self.category_model.calibrated_classifiers_[0].estimator
                
                if hasattr(base_estimator, 'named_steps'):
                    self.vectorizer = base_estimator.named_steps.get('tfidf')
                
                logger.info("✅ Model v6.0 loaded successfully")
            else:
                logger.warning(f"⚠️ NLP Model not found at {model_path}")

        except Exception as e:
            logger.error(f"❌ Failed to load NLP Model: {e}")
            self.category_model = None

    def predict(self, message: str, min_conf: float = 0.35) -> Tuple[bool, str, str, float, str, str]:
        """
        Predict ticket details using exact training logic.
        Returns: (should_create, category, priority, confidence, reason/description, ticket_title)
        """
        if not self.category_model or not self.preprocessor:
            return False, "general", "medium", 0.0, "Model not loaded", ""

        message = str(message)

        # A. Spam / gibberish filter
        if re.search(r'(.)\1{4,}', message) or (len(message) > 20 and ' ' not in message):
            return False, "spam", "low", 0.0, "Spam/Gibberish detected", ""

        # B. Preprocess
        try:
            processed = self.preprocessor.process_batch([message])[0]
        except Exception as e:
            logger.error(f"Preprocessor Error: {e}")
            return False, "error", "medium", 0.0, "Preprocessing failed", ""

        # C. Unknown vocabulary check
        if self.vectorizer:
            try:
                if self.vectorizer.transform([processed]).sum() == 0:
                    return False, "unknown", "low", 0.0, "Unknown vocabulary", ""
            except Exception:
                pass # Skip if check fails

        # D. Category prediction
        try:
            category = self.category_model.predict([processed])[0]
            cat_conf = max(self.category_model.predict_proba([processed])[0])

            if cat_conf < min_conf or str(category).lower() == 'irrelevant':
                return False, str(category), "low", float(cat_conf), "Low confidence or Irrelevant", ""

            # E. Priority prediction (CONTEXT MUST MATCH TRAINING)
            pri_input = f"{processed} [CATEGORY:{category}]"
            priority = self.priority_model.predict([pri_input])[0]
            
            # Map string priority to Enum if needed
            priority_str = str(priority).lower()

            # F. Ticket Title
            ticket_title = f"[{priority_str.upper()}] {str(category).upper()} - {message[:40]}"
            
            logger.info(f"🧠 Prediction: {category.upper()} [{priority_str.upper()}] ({cat_conf:.2f})")
            
            return True, str(category), priority_str, float(cat_conf), f"AI Confidence: {cat_conf:.2f}", ticket_title

        except Exception as e:
            logger.error(f"Prediction Runtime Error: {e}")
            return False, "error", "medium", 0.0, str(e), ""

# Global Instance
ml_guard = MLGuardClassifier()
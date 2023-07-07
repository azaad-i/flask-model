import pyeto
import pyeto.fao
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
import json
import numpy as np
import pandas as pd
from math import sqrt
from numpy import split
from numpy import array
from numpy import concatenate
from pandas import read_csv
from sklearn.metrics import mean_squared_error, mean_absolute_error
from sklearn.metrics import r2_score
from sklearn.metrics import mean_squared_log_error
from sklearn.preprocessing import MinMaxScaler
from matplotlib import pyplot
from keras import regularizers
from keras.models import Sequential
from keras.layers import Dense, Dropout, Flatten, LSTM, Bidirectional
from keras.callbacks import ModelCheckpoint
from keras.models import load_model
from flask import Flask, request, jsonify
import math
from pyeto._check import (
    check_day_hours as _check_day_hours,
    check_doy as _check_doy,
    check_latitude_rad as _check_latitude_rad,
    check_sol_dec_rad as _check_sol_dec_rad,
    check_sunset_hour_angle_rad as _check_sunset_hour_angle_rad,
)
from firebase_admin import credentials, firestore, initialize_app

app = Flask(__name__)

scheduler = BackgroundScheduler(daemon=True)

# Initialize the Firebase app
cred = credentials.Certificate('user-details-3840f-firebase-adminsdk-pbwo6-00a5793239.json')
initialize_app(cred)

# Create a Firestore client
db = firestore.client()

@app.route('/calculate_eto', methods=['POST'])
def calculate_eto():
    # Retrieve data from Firestore collection
    collection_ref = db.collection('hw')
    query = collection_ref.order_by('Date', direction=firestore.Query.DESCENDING).limit(1)
    docs = query.get()
    data = [doc.to_dict() for doc in docs]

    if not data:
        return jsonify({'error': 'No data available'})

    data = data[-1]
    svp = pyeto.fao.svp_from_t(data['Tavg'])
    svpmax = pyeto.fao.svp_from_t(data['Tmax'])
    svpmin = pyeto.fao.svp_from_t(data['Tmin'])
    avp = pyeto.avp_from_rhmean(svpmin, svpmax, data['RHavg'])
    NR = (data["NR"] * 24 * 60 * 60) / 1000000
    T = pyeto.celsius2kelvin(data["Tavg"])
    WS = data["Wind_Spd"]
    del_svp = pyeto.fao.delta_svp(data["Tavg"])
    psy = pyeto.fao.psy_const(pyeto.fao.atm_pressure(33))
    time = data['Date']

    # Calculate ETo using PyETo
    c = np.where(NR > 0, 0.24, 0.96)
    shf = np.where(NR > 0, 0.1 * NR, 0.5 * NR)
    a1 = (0.408 * (NR - shf) * del_svp / (del_svp + (psy * (1 + c * WS))))
    a2 = (37 * WS / T * (svp - avp) * psy / (del_svp + (psy * (1 + c * WS))))
    eto = a1 + a2

    # Store ETo in Firestore collection
    new_collection_ref = db.collection('eto-hourly')
    new_doc_ref = new_collection_ref.document()
    new_doc_ref.set({
        'time': datetime.now(),
        'date': time,
        'eto': eto
    })

    return jsonify({'date': time, 'eto': eto})


@app.route('/ts_model', methods=['POST'])
def prediction():
    # Retrieve data from Firestore collection
    collection_ref = db.collection('ts')
    query = collection_ref.order_by('Date', direction=firestore.Query.DESCENDING).limit(24)
    snapshots = query.stream()
    data = [snapshot.to_dict() for snapshot in snapshots]
    data = sorted(data, key=lambda x: x['Date'])

    if not data:
        return jsonify({'error': 'No data available'})

    date = [element['Date'] for element in data]
    eto = [element['ETo'] for element in data]

    new_date = []
    for dt_str in date:
        dt = datetime.strptime(dt_str, "%d-%m-%Y %H:%M")
        updated_dt = dt + timedelta(hours=24)
        new_date.append(updated_dt.strftime("%d-%m-%Y %H:%M"))

    # Load the trained model
    model = load_model("Aiscr_bilstm_24-24-20230702T130124Z-001/Aiscr_bilstm_24-24")

    # Preprocess the input data
    test = np.array(eto)
    test = test.reshape((-1, 1))
    scaler = MinMaxScaler()
    test = scaler.fit_transform(test)
    test = test.reshape((1, 24, 1))

    # Make predictions using the model
    y_pred = model.predict(test)
    y_pred = scaler.inverse_transform(y_pred)
    y_pred = y_pred.flatten().tolist()

    # Prepare the results dictionary
    joined_dict = {new_date[i]: y_pred[i] for i in range(len(new_date))}

    # Store predictions in Firestore collection
    for key, value in joined_dict.items():
        doc_ref = db.collection('ts').document()
        doc_ref.set({
            'Date': key,
            'ETo': value
        })

    return jsonify(joined_dict)

@app.route('/health')
def health_check():
    return 'OK'


if __name__ == '__main__':
    # Start the scheduler
    scheduler.start()

    # Add the tasks to the scheduler with desired schedules
    scheduler.add_job(calculate_eto, 'interval', hours=1)
    scheduler.add_job(prediction, 'interval', hours=24)

    # Run the Flask application
    app.run(debug=True)

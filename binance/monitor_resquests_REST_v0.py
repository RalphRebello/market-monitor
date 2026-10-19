import requests
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from time import sleep, time


tz = ZoneInfo('America/Sao_Paulo')


class BinanceAPIError(Exception):
    pass


def ms_to_datetime(timestamp):
    return datetime.fromtimestamp(timestamp / 1000, tz)


def get_server_time_offset():
    local_time_0 = int(time() * 1000)    
    server_time  = requests.get('https://api.binance.com/api/v3/time', 
                                timeout=(3.05, 27))
    local_time_1 = int(time() * 1000)

    if server_time.status_code != requests.codes.ok:
        raise BinanceAPIError(f'Invalid server time | '
                              f'StatusCode: {server_time.status_code}')

    time_offset = int((local_time_0 + local_time_1) / 2) - server_time.json()['serverTime']

    return time_offset


def server_now_ms(time_offset):
    return int((time() * 1000) - time_offset)



def get_candles(trade_config, time_offset):
    candles = []
    r = requests.get('https://api.binance.com/api/v3/klines', params=trade_config, timeout=(3.05, 27))
 
    if r.status_code != requests.codes.ok:
        try:
            r_data = r.json()
       
        except requests.exceptions.JSONDecodeError:
            raise BinanceAPIError(f"StatusCode: {r.status_code} | "
                                  f"MSG: {r.text[:200] or 'Empty response'} "
            )
        
        else:
            raise BinanceAPIError(f"FailureCode: {r_data['code']} | "
                                  f"StatusCode {r.status_code} | "
                                  f"MSG: {r_data['msg'].replace('.', ' -> ')}({trade_config['symbol']})\n"
                        )

    r_data = r.json()
    
    server_time = server_now_ms(time_offset)

    for candle_data in r_data:
        candle = {}
        
        candle['open_time']          = candle_data[0]
        candle['open']               = float(candle_data[1])
        candle['high']               = float(candle_data[2])
        candle['low']                = float(candle_data[3])
        candle['close']              = float(candle_data[4])
        candle['volume']             = float(candle_data[5])
        candle['close_time']         = candle_data[6]
        candle['quote_asset_volume'] = float(candle_data[7])
        candle['number_of_trades']   = int(candle_data[8])
        candle['is_closed']          = candle_data[6] < server_time
       
        candles.append(candle)

    return candles


def show_candle(candles, time_offset):
    print('+++++++++++++ Monitoring Candle +++++++++++++')

    candle = {}

    for c in candles:
        if not c['is_closed']:
            candle = c

    if not candle:
        print('All candles are closed!')
        return
            
    now        = ms_to_datetime(server_now_ms(time_offset))
    close_time = ms_to_datetime(candle['close_time'])

    while close_time > now:
    
        seconds_left = (close_time - now).total_seconds()

        print(f"There are {int(seconds_left)} seconds left until the candle closes.")

        print('+++++++++++++++++++++++++++++++++++++++++++++')

        sleep(1)

        now = ms_to_datetime(server_now_ms(time_offset))

    print('Candle closed')
        

def main():
    trade_config = {
                    'symbol'      : 'BTCBRL',
                    'interval'    : '1m',
                    'limit'       : 3,
                    }

    try:
        time_offset = get_server_time_offset()

        candles = get_candles(trade_config, time_offset)

        for candle in candles:
            for key, value in candle.items():
                print(f'{key} : {value}')

            print('\n###################################\n')

        show_candle(candles, time_offset)

    except BinanceAPIError as error:
        print(f'Binance API error: {error}', file=sys.stderr)
        sys.exit(1)

    except requests.exceptions.RequestException as error:
        print(f'Network error: {error}', file=sys.stderr)
        sys.exit(1)

    except KeyboardInterrupt:
        print('\nFINISHED BY USER', file=sys.stderr)
        sys.exit(130)


if __name__ == '__main__':
    main()
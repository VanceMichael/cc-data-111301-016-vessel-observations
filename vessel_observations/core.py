from pathlib import Path


def read_sentences(path):
    with Path(path).open(encoding='ascii') as stream:
        for line_number, line in enumerate(stream,1):
            line = line.strip()
            if not line.startswith('!') or '*' not in line:
                raise ValueError('NMEA 包络不合法')
            body, checksum_text = line[1:].rsplit('*',1)
            checksum = 0
            for character in body:
                checksum ^= ord(character)
            if checksum != int(checksum_text,16):
                raise ValueError('NMEA 校验和不一致')
            fields = body.split(',')
            if len(fields) != 7:
                raise ValueError('NMEA 字段数量不正确')
            yield {'line':line_number,'formatter':fields[0],'total':int(fields[1]),'part':int(fields[2]),
                   'sequence':fields[3],'channel':fields[4],'payload':fields[5],'fill':int(fields[6])}


def inspect_file(path):
    sentences = list(read_sentences(path))
    return {'sentences':len(sentences),'multipart':sum(item['total']>1 for item in sentences)}

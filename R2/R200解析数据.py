
import re
import xlwt


def get_data(scan_length, txt_path):
    with open(txt_path, "rb") as f:
        print('打开文件')
        data_draw = f.read()
    data_draw = str(data_draw, encoding="utf-8").replace(' ', '')
    print(len(data_draw))
    # 提取每一圈的数据分开并写入列表
    pattern = r'\w{%d}' % scan_length * 2
    data_list = re.findall(pattern, data_draw)  # c2系列一点个八个字节 ，r2一个点
    print('数据提取完成')
    data_list = data_list[:20]
    print(len(data_list))
    return data_list


def draw(data_):
    t = 0
    workbook = xlwt.Workbook(encoding='utf-8')
    sheet1 = workbook.add_sheet('距离')
    b = ''
    print(len(data_))
    while t < len(data_):
        b = data_[t]

        while True:  # 提取各包包头和包尾之间的点的数据
            baotou_num = b.find('5CA24300')
            if baotou_num == -1:
                break
            else:
                b = b.replace(b[baotou_num:baotou_num + 152], '')

        # 提取所有点的信息分开并写入列表
        word_tem = re.findall(r'\w{8}', b)

        print(len(word_tem))
        a = ''
        # 提取所有点的距离信息分开并写入列表
        word_dis_a = []
        word_dis_b = []
        for word in word_tem:
            word_a = word[6] + word[7] + word[4]   # 反射率
            word_b = word[5] + word[2] + word[3] + word[0] + word[1]  # 距离
            word_a = int(word_a,16)
            word_dis_a.append(str(word_a))
            word_b = int(word_b, 16)
            word_dis_b.append(str(word_b))

        print(len(word_dis_a))
        y = word_dis_a

        sheet1.write(0, 2*t, '距离')
        sheet1.write(0, 2*t+1, '反射率')
        num_points = len(word_dis_a)  # 使用实际数据点数
        for i in range(num_points):  # 而不是固定7200
            sheet1.write(i+1, 2*t, word_dis_b[i])
            sheet1.write(i + 1, 2*t+1, word_dis_a[i])
        t += 1

        workbook.save(excel_path)
        print('数据写入成功！')


scan_length = 30472
file_indices = list(range(81))
#for i in file_indices:
   # txt_path = f'E:/R2000DATA/alldata/data{i}.txt'
   # excel_path = f'E:/R2000DATA/R2000{i}.xls'

excel_path = r'E:/R2000DATA/R2000.xls'
txt_path = r'E:/R2000DATA/data.txt'
draw(get_data(scan_length, txt_path))

